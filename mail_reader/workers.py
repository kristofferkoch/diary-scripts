"""DB-backed priority queue workers.

One async loop per configured pass. Each loop claims the highest-priority
pending row for its (tier, model) via `FOR UPDATE SKIP LOCKED`, generates
the summary, and updates the row to `done` / `failed`. When the queue is
empty the worker sleeps for a short backoff before polling again.

The DB-as-queue keeps the workers stateless: a restart picks up any
abandoned `streaming` rows (after `RECLAIM_AFTER_SECONDS`, see
`summarize.claim_for_generation`) and continues. Multiple worker
processes are safe — `SKIP LOCKED` ensures each row goes to exactly one
worker. v1 runs one task per pass inside the FastAPI process.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import urllib.error

import anyio.to_thread

from . import db, summarize
from .config import summaries_enabled, summaries_max_tier

log = logging.getLogger(__name__)

IDLE_SLEEP_SECONDS = float(os.environ.get("WORKER_IDLE_SLEEP", "2.0"))
ERROR_SLEEP_SECONDS = float(os.environ.get("WORKER_ERROR_SLEEP", "10.0"))
# How often each worker loop sweeps for stale `streaming` rows whose
# claiming worker died mid-process. Cheap (one indexed UPDATE).
REAPER_INTERVAL_SECONDS = float(os.environ.get("WORKER_REAPER_INTERVAL", "60.0"))


def _claim_next(model: str, tier: int) -> str | None:
    """Atomically grab the highest-priority pending row for (model, tier)
    and flip its status to `streaming`. Returns the message-Id (notmuch
    form, no `<>`) to process, or None if the queue is empty."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE summaries
                SET status = 'streaming'
                WHERE id IN (
                    SELECT s.id
                    FROM summaries s
                    WHERE s.status = 'pending'
                      AND s.quality_tier = %s
                      AND s.model = %s
                    ORDER BY s.priority DESC, s.requested_at DESC, s.id ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING (
                    SELECT m.message_id FROM messages m WHERE m.id = summaries.message_id
                )
                """,
                (tier, model),
            )
            row = cur.fetchone()
            conn.commit()
        return row[0] if row else None


def _process_one(model: str, tier: int) -> bool:
    """Pick one pending row at this tier; run Ollama; persist done/failed.
    Returns True if a row was processed (so the caller loops fast),
    False if the queue is empty (back off)."""
    mid = _claim_next(model, tier)
    if mid is None:
        return False
    log.info("[worker tier=%s model=%s] processing %s", tier, model, mid)
    with db.connect() as conn:
        summarize.generate_and_store(conn, mid, model=model)
    return True


def _reap_stale(max_age_seconds: int) -> int:
    with db.connect() as conn:
        return summarize.reclaim_stale_streaming(conn, max_age_seconds)


async def worker_loop(model: str, tier: int) -> None:
    """Long-running async task. Yields back to the event loop between
    items so the FastAPI app stays responsive. Sync DB + HTTP work
    happens in the default threadpool via `anyio.to_thread.run_sync`."""
    log.info("[worker] started tier=%s model=%s", tier, model)
    last_reap = time.monotonic()
    try:
        while True:
            try:
                processed = await anyio.to_thread.run_sync(
                    _process_one, model, tier,
                )
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                log.warning("[worker tier=%s] transient error: %s — backoff",
                            tier, e)
                await asyncio.sleep(ERROR_SLEEP_SECONDS)
                continue
            except Exception:
                log.exception("[worker tier=%s] unexpected error", tier)
                await asyncio.sleep(ERROR_SLEEP_SECONDS)
                continue
            if time.monotonic() - last_reap >= REAPER_INTERVAL_SECONDS:
                last_reap = time.monotonic()
                n = await anyio.to_thread.run_sync(
                    _reap_stale, summarize.RECLAIM_AFTER_SECONDS,
                )
                if n:
                    log.warning("[reaper tier=%s] failed %s stale streaming row(s)",
                                tier, n)
            if not processed:
                await asyncio.sleep(IDLE_SLEEP_SECONDS)
    except asyncio.CancelledError:
        log.info("[worker] stopped tier=%s model=%s", tier, model)
        raise


def spawn_all() -> list[asyncio.Task]:
    """Launch one worker task per configured pass. Reclaims any rows still
    in `streaming` at boot — those are by definition orphans from a dead
    previous worker process, since spawn_all is the only thing that creates
    workers for this process.

    Returns no workers when summaries are disabled via config — the queue
    is never consumed, so no LLM/GPU work happens (see
    `config.summaries_enabled`)."""
    if not summaries_enabled():
        log.info("[worker] summaries disabled (config) — no workers spawned")
        return []
    n = _reap_stale(0)
    if n:
        log.warning("[reaper] cleaned %s orphaned streaming row(s) at startup", n)
    max_tier = summaries_max_tier()
    tasks = []
    for p in summarize.PASSES:
        if p["tier"] > max_tier:
            log.info("[worker] tier-%s pass (%s) capped out via summaries.max_tier — no worker",
                     p["tier"], p["model"])
            continue
        tasks.append(asyncio.create_task(
            worker_loop(p["model"], p["tier"]),
            name=f"summary-worker-tier{p['tier']}",
        ))
    return tasks

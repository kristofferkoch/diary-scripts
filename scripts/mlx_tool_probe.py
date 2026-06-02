#!/usr/bin/env python3
"""mlx_tool_probe.py — does this MLX model do its job reliably?

Diagnostic harness for evaluating a candidate model + chat-template config.
Two families of scenarios:

  - Tool-calling scenarios (`calendar`) run a small agent loop against
    `mlx_lm.server` to check whether the model routes tool calls correctly.
    Used for the *classifier tier* (Qwen3.6-4bit-DWQ on port 8080).

  - Extraction scenario (`nuextract`) runs single-shot against
    `mlx_vlm.server` with a NuExtract schema-template kwarg. Used for
    the *extractor tier* (NuExtract3-bf16 on port 8081).

Examples:
    # Smoke-test the classifier (tool routing + thinking):
    uv run scripts/mlx_tool_probe.py --scenario calendar

    # Extractor against a real mail thread:
    uv run scripts/mlx_tool_probe.py --scenario nuextract \\
        --mail-thread 0000000000034946

    # Same, but force the old NuExtract-2.0 model:
    PR_COMPOSE_WRITER_MODEL=mlx-community/numind-NuExtract-2.0-8B-MLX \\
        uv run scripts/mlx_tool_probe.py --scenario nuextract \\
        --mail-thread 0000000000034946
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

from mail_reader.config import mlx_url
from scripts.pr_compose import (
    BODY_MAX_CHARS, NUEXTRACT_BASE, WRITER_MODEL, WRITER_SCHEMA,
    _latest_msg_in_thread, _strip_code_fence, _tool_get_calendar_events,
    _writer_payload, canonicalize_for_llm, load_mail,
)

MLX_BASE = mlx_url()  # $MLX_BASE → config hosts.mlx
DEFAULT_MODEL = "mlx-community/Qwen3.6-35B-A3B-4bit-DWQ"
MAX_ROUNDS = 5

# Qwen3.6 official recommended sampling params for THINKING mode, general
# tasks (from huggingface.co/Qwen/Qwen3.6-35B-A3B). The presence_penalty
# at 1.5 is load-bearing — it disincentivizes the model from re-entering
# the "Wait, I should check…" loop attractor we saw at presence_penalty=0.
#
# `presence_context_size` is an MLX extension; we set it large so the
# penalty window covers the full reasoning trace. With the default small
# window (~20-50 tokens) the loop's first repetition falls off before the
# fourth one starts, and the penalty stops protecting.
QWEN_THINKING_PARAMS = {
    "temperature": 1.0,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 1.5,
    "presence_context_size": 4000,
    "repetition_penalty": 1.0,
    "repetition_context_size": 4000,
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_calendar_events",
            "description": (
                "Look up events on Example's calendar (CALENDAR.md and "
                "CALENDAR-PAST.md) that overlap a date range. Use to verify "
                "whether a proposed event is already on the calendar."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "ISO YYYY-MM-DD"},
                    "end_date":   {"type": "string", "description": "ISO YYYY-MM-DD"},
                },
                "required": ["start_date", "end_date"],
            },
        },
    },
]

SCENARIOS = {
    "calendar": {
        "system": (
            "You help Example keep his calendar consistent. When a user "
            "mentions a date or proposes an event, look up the existing "
            "calendar via the get_calendar_events tool BEFORE answering, so "
            "you can warn about overlaps. Reply in Norwegian. End your final "
            "reply with a short line like 'Konklusjon: ...'."
        ),
        "user": (
            "Astrid foreslår at vi har familiebesøk hjemme lørdag 13. juni 2026. "
            "Sjekk om vi allerede har noe i kalenderen den dagen, og svar med "
            "anbefaling."
        ),
    },
}


def _execute_tool(name: str, arguments_json: str) -> str:
    """Dispatch a tool call. Returns the tool result as a string (for the
    tool-result message)."""
    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"arguments not valid JSON: {e}"})
    if name == "get_calendar_events":
        return _tool_get_calendar_events(
            args.get("start_date", ""), args.get("end_date", ""),
        )
    return json.dumps({"error": f"unknown tool: {name}"})


def _post_chat(client: httpx.Client, base: str, payload: dict) -> dict:
    # 32k-token Qwen thinking generations can take ~10 min wall on 6-bit;
    # NuExtract is faster but still allow generous headroom so we measure
    # the model, not the client.
    r = client.post(f"{base}/v1/chat/completions", json=payload, timeout=1200)
    r.raise_for_status()
    return r.json()


# ---------- Tool-calling probe (classifier tier)

def run_tool_probe(model: str, scenario: str, enable_thinking: bool,
                   max_tokens: int) -> int:
    sc = SCENARIOS[scenario]
    messages = [
        {"role": "system", "content": sc["system"]},
        {"role": "user", "content": sc["user"]},
    ]
    user_preview = sc["user"][:80]

    print(f"# probe: model={model}  base={MLX_BASE}")
    print(f"#        scenario={scenario} thinking={enable_thinking} "
          f"max_tokens={max_tokens}")
    print(f"#        user: {user_preview}...")
    print()

    client = httpx.Client()
    rounds = 0
    total_tokens = 0
    started = time.monotonic()
    while rounds < MAX_ROUNDS:
        rounds += 1
        payload = {
            "model": model,
            "messages": messages,
            "tools": TOOL_SCHEMAS,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": enable_thinking},
            **QWEN_THINKING_PARAMS,
        }
        round_started = time.monotonic()
        resp = _post_chat(client, MLX_BASE, payload)
        round_elapsed = time.monotonic() - round_started
        choice = resp["choices"][0]
        msg = choice["message"]
        finish = choice.get("finish_reason")
        usage = resp.get("usage", {})
        total_tokens += usage.get("completion_tokens", 0)

        print(f"--- round {rounds}  ({round_elapsed:.1f}s, "
              f"{usage.get('completion_tokens', '?')} tok, "
              f"finish={finish}) ---")

        tool_calls = msg.get("tool_calls") or []
        content = (msg.get("content") or "").strip()
        reasoning = (msg.get("reasoning") or "").strip()
        if reasoning:
            print(f"  reasoning ({len(reasoning)} chars): "
                  f"{reasoning[:200].replace(chr(10), ' ')}...")

        if tool_calls:
            messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            })
            for tc in tool_calls:
                fname = tc["function"]["name"]
                fargs = tc["function"].get("arguments", "{}")
                print(f"  → tool call: {fname}({fargs})")
                result = _execute_tool(fname, fargs)
                result_short = result[:200].replace("\n", " ")
                print(f"    result: {result_short}{'…' if len(result) > 200 else ''}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": fname,
                    "content": result,
                })
            continue

        if not content:
            print("  !! empty content, no tool calls — model gave up "
                  "(likely truncated mid-thinking)")
            if reasoning:
                print("  --- full reasoning (truncated mid-thinking) ---")
                for line in reasoning.splitlines():
                    print(f"  | {line}")
                print("  --- end reasoning ---")
            elapsed = time.monotonic() - started
            print(f"\n# FAIL: empty final answer after {rounds} round(s), "
                  f"{elapsed:.1f}s, {total_tokens} completion tokens")
            return 2
        print(f"  final content ({len(content)} chars):")
        for line in content.splitlines():
            print(f"    {line}")
        elapsed = time.monotonic() - started
        ok = finish == "stop"
        verdict = "OK" if ok else f"PARTIAL (finish={finish})"
        print(f"\n# {verdict}: {rounds} round(s), {elapsed:.1f}s, "
              f"{total_tokens} completion tokens")
        return 0 if ok else 1

    elapsed = time.monotonic() - started
    print(f"\n# FAIL: hit MAX_ROUNDS={MAX_ROUNDS} without a final answer "
          f"({elapsed:.1f}s, {total_tokens} completion tokens)")
    return 3


# ---------- NuExtract probe (extractor tier)

def run_nuextract_probe(model: str, mail_thread: str) -> int:
    """Single-shot extraction against a real notmuch thread. Reuses
    pr_compose's WRITER_SCHEMA and _writer_payload so this probe stays
    in sync with what the production pipeline sends."""
    msg_id = _latest_msg_in_thread(mail_thread)
    if not msg_id:
        raise SystemExit(f"thread:{mail_thread} has no resolvable message")
    row = load_mail(msg_id)
    if row is None:
        raise SystemExit(f"failed to load id:{msg_id}")

    body = canonicalize_for_llm(row.body[:BODY_MAX_CHARS])
    subject = canonicalize_for_llm(row.subject)
    document = f"Subject: {subject}\nFrom: {row.sender}\n\n{body}"
    payload = _writer_payload(model, document, WRITER_SCHEMA)

    print(f"# probe: model={model}  base={NUEXTRACT_BASE}")
    print(f"#        scenario=nuextract  thread:{mail_thread}")
    print(f"#        subject: {subject[:80]}")
    print(f"#        from:    {row.sender[:80]}")
    print(f"#        body:    {len(body)} chars")
    print(f"#        schema:  {json.dumps(WRITER_SCHEMA)[:120]}…")
    print()

    started = time.monotonic()
    client = httpx.Client()
    resp = _post_chat(client, NUEXTRACT_BASE, payload)
    elapsed = time.monotonic() - started

    choice = resp["choices"][0]
    msg = choice["message"]
    finish = choice.get("finish_reason")
    usage = resp.get("usage", {})
    content = (msg.get("content") or "").strip()

    print(f"# response: finish={finish}  {usage.get('completion_tokens', '?')} tok  "
          f"{elapsed:.1f}s")

    if not content:
        print("  !! empty content from extractor")
        print(f"\n# FAIL: empty extraction ({elapsed:.1f}s)")
        return 2

    print(f"  raw content ({len(content)} chars):")
    for line in content.splitlines():
        print(f"    {line}")

    cleaned = _strip_code_fence(content)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"\n# FAIL: content is not valid JSON ({e})")
        return 1

    print()
    print("  --- parsed ---")
    for k in ("pr_title", "branch_keyword", "memory_heading"):
        print(f"    {k}: {parsed.get(k)!r}")
    body_out = parsed.get("memory_body", "") or ""
    print(f"    memory_body: {body_out[:120]!r}{'…' if len(body_out) > 120 else ''}")
    candidates = parsed.get("calendar_candidates") or []
    print(f"    calendar_candidates ({len(candidates)}):")
    for c in candidates:
        print(f"      • {c.get('date', '')} — {c.get('title', '')}  "
              f"({(c.get('evidence', '') or '')[:60]})")

    # Schema sanity: all required keys present?
    required = {"pr_title", "branch_keyword", "memory_heading",
                "memory_body", "calendar_candidates"}
    missing = required - set(parsed.keys())
    if missing:
        print(f"\n# PARTIAL: missing keys {missing}")
        return 1

    print(f"\n# OK: extraction complete ({elapsed:.1f}s)")
    return 0


# ---------- Main

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", default=None,
                    help="Default depends on --scenario: classifier model for "
                         "tool scenarios, WRITER_MODEL (NuExtract3) for nuextract")
    ap.add_argument("--scenario", default="calendar",
                    choices=list(SCENARIOS) + ["nuextract"])
    ap.add_argument("--mail-thread", default=None,
                    help="notmuch thread id (without 'thread:' prefix) — "
                         "required when --scenario nuextract")
    ap.add_argument("--no-thinking", dest="thinking", action="store_false",
                    default=True,
                    help="Disable enable_thinking on tool scenarios (Qwen3.6 "
                         "needs thinking ON for reliable tool routing — use "
                         "this only to confirm the failure mode). Ignored for "
                         "nuextract.")
    ap.add_argument("--max-tokens", type=int, default=4000)
    args = ap.parse_args(argv)

    if args.scenario == "nuextract":
        if not args.mail_thread:
            raise SystemExit("--scenario nuextract requires --mail-thread <id>")
        model = args.model or WRITER_MODEL
        return run_nuextract_probe(model, args.mail_thread)

    model = args.model or DEFAULT_MODEL
    return run_tool_probe(model, args.scenario, args.thinking, args.max_tokens)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

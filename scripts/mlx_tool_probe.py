#!/usr/bin/env python3
"""mlx_tool_probe.py — does this MLX model handle tool-calling reliably?

Diagnostic harness for evaluating a candidate model + chat-template config
against the OpenAI tool-use protocol. Runs a small agent loop with one or
two built-in tools, prints what happened each round, and reports whether
the model completed without truncation.

Use when:
  - A new model lands and you want to know if it's a viable writer
  - Bumping quant (4-bit-DWQ → 6-bit) to see if tool routing improves
  - Adjusting `enable_thinking` and want a fast yes/no on completion

Examples:
    # Default scenario, 6-bit, thinking on (Qwen3.6 needs it for tools):
    uv run scripts/mlx_tool_probe.py --model mlx-community/Qwen3.6-35B-A3B-6bit

    # Compare against 4-bit-DWQ baseline:
    uv run scripts/mlx_tool_probe.py --model mlx-community/Qwen3.6-35B-A3B-4bit-DWQ

    # Without thinking (Qwen3.6 will usually fail to call tools at all):
    uv run scripts/mlx_tool_probe.py --no-thinking
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import httpx

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from scripts.pr_compose import (  # noqa: E402
    BODY_MAX_CHARS, WRITER_SYSTEM, _latest_msg_in_thread, _tool_get_calendar_events,
    canonicalize_for_llm, load_mail,
)

MLX_BASE = os.environ.get("MLX_BASE", "http://gpu-host:8080")
DEFAULT_MODEL = "mlx-community/Qwen3.6-35B-A3B-6bit"
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


def _load_writer_messages(thread_id: str) -> list[dict]:
    """Build (system, user) for the writer scenario from a real notmuch thread.
    Uses the *same* WRITER_SYSTEM as pr_compose, but augmented with an
    instruction to call get_calendar_events for date verification (in the
    Python-side-dedup default, that's done after the model responds; here
    we want the model to do it via tool calls)."""
    msg_id = _latest_msg_in_thread(thread_id)
    if not msg_id:
        raise SystemExit(f"thread:{thread_id} has no resolvable message")
    row = load_mail(msg_id)
    if row is None:
        raise SystemExit(f"failed to load id:{msg_id}")
    system = (
        WRITER_SYSTEM
        + "\n\n"
        "Additionally: for every entry in `calendar_candidates`, you MUST "
        "call the `get_calendar_events` tool to check whether the proposed "
        "date overlaps something already in CALENDAR.md / CALENDAR-PAST.md. "
        "Set `already_in_calendar` based on the tool result. Do this BEFORE "
        "emitting the final JSON. If the email has no dates, emit "
        "`calendar_candidates: []` without any tool calls."
    )
    body = canonicalize_for_llm(row.body[:BODY_MAX_CHARS])
    subject = canonicalize_for_llm(row.subject)
    user = f"Subject: {subject}\nFrom: {row.sender}\n\n{body}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# Lenient line-oriented format. Hypothesis: strict JSON triggers Qwen3.6's
# thinking-mode reasoning explosion (the model feels obliged to plan every
# field before emitting any token, since one stray char breaks the parse).
# A line-format with CAPS headers lets it stream content section by section
# without that planning overhead, while still being mechanically parseable.
#
# `{today}` is substituted by _writer_lenient_system() at call time so the
# model has an absolute anchor for relative date references in the mail
# ("neste fredag", "i morgen").
WRITER_LENIENT_SYSTEM_TEMPLATE = """\
You draft a section for Example's daily memory file
(`memory/YYYY-MM-DD.md`, today is `memory/{today}.md`) based on an email
he received. Header keywords (TITLE, BRANCH, …) are English ASCII;
content language is free.

Output is line-oriented. Use uppercase headers, one per line, followed by
their value on the same line (single-line fields) or on the lines below
(multi-line fields). The body and dates sections continue until the next
uppercase header.

  TITLE: <short title, max 70 chars>
  BRANCH: <short-kebab-case-ascii-slug>
  HEADING: <heading without the '## ' prefix>
  BODY:
  <2-6 sentences, markdown; use bullet points (-) for action items.
  Multiple lines allowed.>
  DATES:
  - YYYY-MM-DD | <short title> | <quote or reference to email context>
  - YYYY-MM-DD | <short title> | <quote or reference to email context>

If the email has no dates, write `DATES:` on its own line with no entries
below (or simply end the response after BODY).

Calendar verification: for every line under DATES, you MUST call the
`get_calendar_events` tool BEFORE writing it, to check whether the date
overlaps something already in CALENDAR.md / CALENDAR-PAST.md. If you find
an overlap, append ` [OVERLAP]` to the date line. Example:
  - 2026-06-02 | Mulig streik Spiker'n | "fra mandag 02.06" [OVERLAP]

Don't wrap your output in code fences. Don't add commentary after DATES.
Start your response directly with `TITLE:`.
"""


def _writer_lenient_system(today: date | None = None) -> str:
    today = today or date.today()
    return WRITER_LENIENT_SYSTEM_TEMPLATE.format(today=today.isoformat())


def _load_writer_lenient_messages(thread_id: str) -> list[dict]:
    """Like _load_writer_messages but with the lenient line-oriented system
    prompt instead of the JSON one."""
    msg_id = _latest_msg_in_thread(thread_id)
    if not msg_id:
        raise SystemExit(f"thread:{thread_id} has no resolvable message")
    row = load_mail(msg_id)
    if row is None:
        raise SystemExit(f"failed to load id:{msg_id}")
    body = canonicalize_for_llm(row.body[:BODY_MAX_CHARS])
    subject = canonicalize_for_llm(row.subject)
    user = f"Subject: {subject}\nFrom: {row.sender}\n\n{body}"
    return [
        {"role": "system", "content": _writer_lenient_system()},
        {"role": "user", "content": user},
    ]


_HEADER_RE = __import__("re").compile(r"^([A-Z][A-Z_]+):\s*(.*)$")


def parse_lenient(text: str) -> dict:
    """Lenient parser for the line-oriented writer format. Returns a dict
    with the same shape as the JSON writer (pr_title, branch_keyword,
    memory_heading, memory_body, calendar_candidates). Permissive:
    unknown headers are stored under their lowercased key, missing
    headers stay absent, the body/dates blocks accept the model adding
    extra commentary or whitespace."""
    text = text.strip()
    # Strip code fences if the model added them despite being told not to.
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            key = m.group(1).lower()
            inline = m.group(2)
            current = key
            sections[current] = []
            if inline.strip():
                sections[current].append(inline)
        elif current is not None:
            sections[current].append(line)

    def _joined(key: str) -> str:
        return "\n".join(sections.get(key, [])).strip()

    candidates: list[dict] = []
    for line in sections.get("dates", []):
        line = line.strip()
        if not line.startswith("-"):
            continue
        body = line.lstrip("-").strip()
        overlap = False
        if body.endswith("[OVERLAP]"):
            overlap = True
            body = body.removesuffix("[OVERLAP]").rstrip()
        parts = [p.strip() for p in body.split("|")]
        # Tolerate 1-3 segments: date alone, date+title, date+title+evidence.
        date_part  = parts[0] if len(parts) >= 1 else ""
        title_part = parts[1] if len(parts) >= 2 else ""
        evid_part  = parts[2] if len(parts) >= 3 else ""
        if not date_part:
            continue
        candidates.append({
            "date": date_part,
            "title": title_part,
            "evidence": evid_part.strip('"\''),
            "already_in_calendar": overlap,
        })

    return {
        "pr_title": _joined("title"),
        "branch_keyword": _joined("branch"),
        "memory_heading": _joined("heading"),
        "memory_body": _joined("body"),
        "calendar_candidates": candidates,
        # Keep the raw text around for the probe report.
        "_raw": text,
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


def _post_chat(client: httpx.Client, payload: dict) -> dict:
    # 32k-token Qwen thinking generations can take ~10 min wall on 6-bit;
    # give plenty of headroom so we measure the model, not the client.
    r = client.post(f"{MLX_BASE}/v1/chat/completions", json=payload, timeout=1200)
    r.raise_for_status()
    return r.json()


def run_probe(model: str, scenario: str, enable_thinking: bool,
              max_tokens: int, mail_thread: str | None = None) -> int:
    if scenario == "writer":
        if not mail_thread:
            raise SystemExit("--scenario writer requires --mail-thread <id>")
        messages = _load_writer_messages(mail_thread)
        user_preview = messages[1]["content"][:80]
    elif scenario == "writer_lenient":
        if not mail_thread:
            raise SystemExit("--scenario writer_lenient requires --mail-thread <id>")
        messages = _load_writer_lenient_messages(mail_thread)
        user_preview = messages[1]["content"][:80]
    else:
        sc = SCENARIOS[scenario]
        messages = [
            {"role": "system", "content": sc["system"]},
            {"role": "user", "content": sc["user"]},
        ]
        user_preview = sc["user"][:80]

    print(f"# probe: model={model}")
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
        resp = _post_chat(client, payload)
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
        # MLX puts thinking-mode reasoning in a separate field when present.
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

        # No tool calls — should be final answer.
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
        if scenario == "writer_lenient":
            parsed = parse_lenient(content)
            print()
            print("  --- parsed lenient ---")
            print(f"    pr_title:       {parsed['pr_title']!r}")
            print(f"    branch_keyword: {parsed['branch_keyword']!r}")
            print(f"    memory_heading: {parsed['memory_heading']!r}")
            print(f"    memory_body:    {parsed['memory_body'][:120]!r}{'…' if len(parsed['memory_body']) > 120 else ''}")
            print(f"    calendar_candidates ({len(parsed['calendar_candidates'])}):")
            for c in parsed["calendar_candidates"]:
                marker = "⚠" if c["already_in_calendar"] else "⊕"
                print(f"      {marker} {c['date']} — {c['title']} ({c['evidence'][:60]})")
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


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--scenario", default="calendar",
                    choices=list(SCENARIOS) + ["writer", "writer_lenient"])
    ap.add_argument("--mail-thread", default=None,
                    help="notmuch thread id (without 'thread:' prefix) — "
                         "required when --scenario writer")
    ap.add_argument("--no-thinking", dest="thinking", action="store_false",
                    default=True,
                    help="Disable enable_thinking (Qwen3.6 needs thinking "
                         "ON for reliable tool routing — use this only to "
                         "confirm the failure mode)")
    ap.add_argument("--max-tokens", type=int, default=4000)
    args = ap.parse_args(argv)
    return run_probe(args.model, args.scenario, args.thinking,
                     args.max_tokens, args.mail_thread)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

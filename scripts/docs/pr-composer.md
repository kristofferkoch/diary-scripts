# Per-mail PR composer — `pr_compose.py`

> Index: [scripts/](../README.md)

Auto-opens one GitHub PR per "significant" incoming mail, so durable facts
flow from inbox → memory archive without manual transcription. State via
notmuch tags (`pr::triaged`, `pr::significant`, `pr::skip`, `pr::filed`).
Two phases per run:

1. **Classify** new mail (notmuch query or `--since-cursor`). Triage filter
   (skip noise tags + GitHub-notification senders, see "Loop-break filter"
   below) → MLX classifier → tag the thread.
2. **File PRs** for `pr::significant` threads not yet `pr::filed`. Calls the
   writer model, drafts a memory section, opens a worktree, commits as the
   bot, pushes via embedded-PAT URL, opens PR via `gh pr create`.

```bash
# Just see what would happen on recent mail (no tags written, no PRs):
uv run python -m scripts.pr_compose --since-cursor

# Classify + tag, but don't open PRs:
uv run python -m scripts.pr_compose --apply --since-cursor

# Full pipeline — classify, tag, file PRs:
uv run python -m scripts.pr_compose --apply --file-prs --since-cursor

# File PRs only (skip classify), capped at 2:
uv run python -m scripts.pr_compose --file-prs --apply --limit-prs 2 'id:none'
```

### Model server

`mlx_lm.server` on **`gpu-host:8080`** (OpenAI-compatible, runs on the
Mac Studio M3 Ultra). Models selected per request via the `model` field;
**switching is expensive** (~30+ s cold load), so the pipeline batches all
classifier calls before any writer calls. Env vars: `MLX_BASE`,
`PR_COMPOSE_CLASSIFIER_MODEL`, `PR_COMPOSE_WRITER_MODEL`.

### Writer-tier — pivoting from Qwen3.6 to NuExtract (2026-05-28)

**Original plan**: dense writer (Qwen3.6-35B-A3B at 6-bit) drafts a memory
section + calendar candidates per significant mail, calling a
`get_calendar_events` tool via OpenAI tool-use protocol.

**What blocked it**: Qwen3 thinking-mode + tool-calling is a confirmed
upstream bug —
[Qwen3 #1817](https://github.com/QwenLM/Qwen3/issues/1817) (~60 % failure
rate on tool-call emission) and
[vllm #18819](https://github.com/vllm-project/vllm/issues/18819) (thinking-off
+ guided JSON breaks output). The specific failure mode on our prompts
correlates with **content thinness**: rich source mails (multi-date
construction plan) sometimes work; short mails (5-line invitation) always
loop. The model fixates on phantom inconsistencies in its own `<think>`
block and never terminates. Tested-and-failed mitigations: max_tokens up to
32 k (Qwen's recommended budget); Qwen-official sampling
(`presence_penalty=1.5`, `top_p=0.95`, `temperature=1.0`); JSON vs
lenient line-format output; trimmed vs richer system prompts; sandwich
prompting (instructions before AND after the input); typographic
canonicalization of source mail. Adding more context made it
*worse* — the model invents constraints from the additional vocabulary.

**Current pivot**: [NuMind NuExtract-2.0-8B](https://huggingface.co/numind/NuExtract-2.0-8B)
— purpose-built schema-guided extraction, QwenVL-based, multilingual
(Norwegian OK), no thinking mode, designed for "fill this JSON schema from
this document". NuMind claims it beats GPT-4.1 by 9 F1 on extraction with
very low hallucination ([blog](https://numind.ai/blog/outclassing-frontier-llms----nuextract-2-0-takes-the-lead-in-information-extraction)).
In evaluation as of 2026-05-28.

**Planned architecture**:

1. **Classifier** — Qwen3.6-35B-A3B-4bit-DWQ (already deployed, fine at yes/no
   without thinking)
2. **Extractor** — NuExtract-2.0-8B emits `{title, branch, heading, body,
   calendar_candidates}` against a fixed JSON schema
3. **Calendar dedup** — Python-side `_verify_candidates()` (deterministic,
   already implemented, stays as-is)
4. *(Optional)* small instruct model for body fluency if NuExtract's prose
   feels too templated

### Diagnostic harness — `mlx_tool_probe.py`

Standalone harness for evaluating a candidate model + chat-template config
against the OpenAI tool-use protocol. Built during the Qwen3.6 experiments;
useful for any future model evaluation. Scenarios: `calendar` (simple
single-tool agent loop), `writer` (full WRITER_SYSTEM with one mail thread),
`writer_lenient` (line-format output experiment). Always read the model card
on HuggingFace BEFORE picking sampling params — the Qwen3.6 32 k
recommended `max_tokens` was buried in "Best Practices" and we missed it
for half a day.

```bash
# Sanity-check tool-calling on a new model:
uv run python -m scripts.mlx_tool_probe --model <model-id>

# Real writer scenario on a notmuch thread:
uv run python -m scripts.mlx_tool_probe --model <id> --scenario writer_lenient \
    --mail-thread <thread-id-without-prefix> --max-tokens 4000
```

### GitHub bot

Bot account `exampleuser-bot` is a collaborator on `exampleuser/diary`
with Write. Classic PAT (`repo` scope only, 90 d, created 2026-05-27) at
`pass show github/mailbot-pat`. Rotation reminder at 2026-08-25 in
CALENDAR.md. Bot mail goes to `bot@example.com` and Proton filters
it to folder `Botmail` — user's own inbox never sees PR-creation notifications.

Branch protection on `master` is wanted but blocked by GitHub's paywall
($4/mo Pro). Defenses are layered: triage filter on PR-creation
notifications + bot-mail folder routing. Off-premise forge migration
(Codeberg / GitLab.com / self-hosted Forgejo) is the longer-term answer.

### Postgres read-only role

`mailvec_ro` (NOLOGIN base) + `llm_pr_composer` (LOGIN, password at
`pass show pg/llm_pr_composer`) provisioned via
`migrations/009_readonly_llm.sql`. `statement_timeout=15s`,
`idle_in_transaction=30s`. Same `mailvec_ro` base backs all future
LLM-driven SQL consumers — give each its own LOGIN user inheriting from
`mailvec_ro`.

### Loop-break filter — CRITICAL

Opening a PR generates GitHub notification mail. Without filtering, the
next `mail-sync.timer` run sees it, the classifier may flag it as
significant, and the pipeline opens PRs about its own PRs. The triage
layer (`SKIP_SENDER_SUBSTRINGS` in `pr_compose.py`) filters
`notifications@github.com` and `noreply@github.com` BEFORE invoking the
model — the loop breaks regardless of how the model decides.
Belt-and-suspenders with the bot-mail folder routing.

### Future: relative-date resolution via tool calling

Real example we hit on 2026-05-28: Sommerskolen sent a reminder
saying "én uke før kursstart kan du logge inn på våre nettsider og lese
velkomstbrev". The course start is `2026-06-29` (already in
`CALENDAR.md`), so the implied date is `2026-06-22` — but **the
extractor only sees the mail body**, not `CALENDAR.md`, and cannot
resolve "kursstart" to a concrete date. Tier 3 has the same blindspot.

The unlock is a model that can reliably tool-call against a calendar-
lookup tool (the Python-side `_tool_get_calendar_events` we already
have is the right shape for this). The agent loop: extractor produces
calendar candidates + unresolved relative references; a resolution
tier with tool access looks up the anchor events, does the arithmetic,
and appends concrete dates. We attempted this with Qwen3.6 and hit the
upstream tool-calling reliability cliff (thinking-on loops, thinking-
off skips tools — Qwen3 #1817, vllm #18819).

The monthly model-check (`CALENDAR.md` → Recurring månedlig → "Sjekk
pipeline-modeller + server-software") is the explicit gate for re-
attempting this feature. Don't pick it up in isolation; the chance of
a usable candidate is highest right after a model that scores well on
the tool-calling-judgment benchmark below appears.

Worth tracking because this class of dates ("X dager før Y", "uken
etter Z", "før sommerferien") is common in school mail, contractor
mail, and event invites. Catching them automatically would turn the
calendar from "what the mail literally mentions" into "what the mail
implies given the existing schedule".

**Where to actually look for candidates** (curated 2026-05-28; the
landscape moves — review during the monthly check):

- [`huggingface.co/mlx-community`](https://huggingface.co/mlx-community)
  — anything that lands here can run on `gpu-host` immediately. Sort
  by "Recently Created", or subscribe via
  [`zernel/huggingface-trending-feed`](https://github.com/zernel/huggingface-trending-feed)
  RSS.
- [`huggingface.co/numind`](https://huggingface.co/numind) — for
  future NuExtract versions (4? 5?). Same chat-template-kwargs protocol
  as 2.0/3, already validated for our tier 2.
- [`huggingface.co/Qwen`](https://huggingface.co/Qwen) — for future
  Qwen versions. Qwen 3.7 or 4 with the tool-calling bug fixed is the
  most likely unlock for tier 2.5. Check Qwen3 issue #1817 for fix
  status before assuming a new release helps.
- [`lintware/tool-calling-benchmark`](https://github.com/lintware/tool-calling-benchmark)
  — fork of MikeVeerman's benchmark with **MLX + llama.cpp backends on
  Apple Silicon**. Measures *judgment* (when to call), not just
  *execution* (whether the JSON parses) — exactly the metric tier 2.5
  needs. Run this against any candidate before wiring it in. Surprise
  finding from earlier rounds: `qwen3:1.7b` topped the benchmark at
  0.960 Agent Score — small models can do this well.
- [Simon Willison's newsletter](https://simonw.substack.com/) — high-
  signal weekly digest of LLM releases. Covers local models, tool
  calling, multimodal — all relevant to our pipeline.
- [r/LocalLLaMA](https://www.reddit.com/r/LocalLLaMA/) — community
  where benchmarks like lintware's go viral first. High noise, but
  also where "this new model is actually different" posts surface
  before aggregators catch them.

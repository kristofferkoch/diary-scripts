# Finance — `finance_ingest.py`

> Index: [scripts/](../README.md)

Summarise a Bulder Bank CSV export and cross-reference it with the embedded
mail archive.

Bulder is mobile-only; there's no API. The user exports from the iOS app
share-sheet. **Two delivery routes are in active use:**

### Route A — Signal note-to-self (current default)

The user shares the CSV to their own Signal number. The file lands as a
`text/comma-separated-values` attachment grabbed by `signal-capture`, with a
filename like `transactions_export_YYYYMMDD_HHMMSS_NNN.csv` (note: this
differs from the mail route's `eksporterte_transaksjoner.csv`). The CSV bytes
are stored under `~/.local/share/signal-cli/attachments/<id>.csv`.

```bash
# 1. Spot the CSV in the Signal triage pass:
uv run signalshow --since-cursor --headers-only     # look for text/comma-separated-values

# 2. Find the attachment id (the JSONL carries metadata only, not bytes):
grep 'comma-separated-values' memory/signal/$(date -u +%Y-%m-%d).jsonl
#   → "id": "E2qcJQbNhCFTpk6tqWrN.csv" in the attachments array

# 3. Download through the restricted proxy, then run the ingester:
uv run signal-attachment '<id>' --output memory/signal/<id>
uv run finance-ingest memory/signal/<id>
uv run finance-ingest memory/signal/<id> --enrich
```

The Signal route has been the actual default since mid-2026 (see `FINANCE.md`
ingestion log: 2026-06-11, 2026-07-03, 2026-08-04 all arrived this way).
`finance-ingest` has no `--from-signal` flag — pass the path positionally.
`signalshow --bump` after triage as usual.

### Route B — mail

The user mails the CSV to themselves. Subject is `Bulder bank eksport`,
attachment `eksporterte_transksjoner.csv`, semicolon-separated with Norwegian
comma decimals. `--from-mail` auto-extracts the newest such mail.

```bash
# From a CSV on disk
uv run finance-ingest /tmp/bulder/eksporterte_transaksjoner.csv

# Auto-extract latest "Bulder bank eksport" mail and summarise
uv run finance-ingest --from-mail

# Same, plus cross-reference vs embedded mail
PG_DSN=dbname=mailvec uv run finance-ingest --from-mail --enrich
```

Output (markdown tables on stdout): per-month inn/ut, per-account outflows,
top-25 merchants by `Tekst`, recurring (≥2 months), large one-offs (≥20k NOK).

**Bulder's `Hovedkategori` / `Underkategori` columns are mostly garbage** — the
summary leans on `Tekst` (merchant) and `Dato` instead. Don't trust the auto-
categorization.

**`--enrich`:** for each "interesting" transaction (large / unlabelled /
Ukategorisert), finds matching mail by (a) amount-match against extracted
money entities in postgres, (b) notmuch search on date±3d + merchant keyword,
and lists the matches per transaction.

The matches displayed in the output are deliberately broad (any mail in the
date window with a merchant-keyword hit) — the value isn't precise correlation,
it's that the bank ledger marks those mails as worth a closer look.

### Before flagging an "unidentified" large transaction

`--enrich` only matches against **mail**. The workspace has several other
places a transaction may already be documented, and **the bank's merchant
label often differs from the receipt/vendor name** (e.g. bank "G-SPORT ÅL AS"
↔ receipt "Intersport Ål"; bank "JAKOL" ↔ `USER.md` "Basebeton / Jakol AS",
Piotr Sitarski). Before listing a large out-payment as an open follow-up,
grep across:

- `USER.md` — known contractors, vendors, family contacts (often with the
  legal entity name that matches what the bank sees)
- `RECEIPTS.md` — paper-receipt register, indexed by date and vendor
- `FDV_*.md` — property work, often paired with vendor names + dates
- `memory/YYYY-MM-DD.md` for the transaction date ± a few days
- `CALENDAR-PAST.md` — multi-day work windows (e.g. floor renovation)

Cadence: monthly, see `CALENDAR.md`. Always reuse the existing pipeline:
`mail_reader.db.connect()`, `scripts.embed_mail.embed_batch` — don't
reimplement DSN / embedding.

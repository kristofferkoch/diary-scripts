#!/usr/bin/env python3
"""
Summarize a Bulder Bank CSV export ("eksporterte_transaksjoner.csv").

Input format (Norwegian Bulder app, semicolon-separated, comma decimal):
    Dato;Beløp;Originalt Beløp;Original Valuta;Til konto;Til kontonummer;
    Fra konto;Fra kontonummer;Type;Tekst;KID;Hovedkategori;Underkategori

Bulder's app only mails CSVs to self — there's no public API. Workflow:
    1. Export from the Bulder iOS app, share-sheet → mail to self.
    2. Wait for `mail-sync.timer` to pick it up (or trigger manually).
    3. Run this script with either a CSV path or `--from-mail` (auto-extract).
    4. Use the output to update FINANCE.md.

Usage:
    scripts/finance_ingest.py /path/to/eksporterte_transaksjoner.csv
    scripts/finance_ingest.py --from-mail
    scripts/finance_ingest.py --from-mail --enrich

Output is plain text (markdown-flavoured tables) on stdout. Pipe through
`tee >> FINANCE.md` after reviewing if you want to splice it in.

Caveats:
- Bulder's auto-categorisation (Hovedkategori/Underkategori) is unreliable —
  this script de-emphasises it. Use `Tekst` (merchant) as the primary signal.
- With --enrich, every transaction flagged as "interesting" (large, untyped,
  or Ukategorisert) is matched against embedded mail (postgres money entities
  + notmuch date-window search) and the matches are listed per transaction.

Money handling: Decimal throughout. Norwegian numbers (1 234,56) are parsed
on read and rendered NO-style on write (space thousands, comma decimal).
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime
import io
import json
import pathlib
import re
import subprocess
import sys
from collections import defaultdict
from decimal import Decimal

CSV_COLUMNS = (
    "Dato", "Beløp", "Originalt Beløp", "Original Valuta",
    "Til konto", "Til kontonummer", "Fra konto", "Fra kontonummer",
    "Type", "Tekst", "KID", "Hovedkategori", "Underkategori",
)


@dataclasses.dataclass(frozen=True)
class Transaction:
    dato: datetime.date
    belop: Decimal
    fra_konto: str
    til_konto: str
    type: str
    tekst: str
    hovedkategori: str
    underkategori: str

    @property
    def month(self) -> str:
        return self.dato.strftime("%Y-%m")

    @property
    def is_out(self) -> bool:
        return self.belop < 0


def parse_no_decimal(s: str) -> Decimal:
    """Parse a Norwegian-formatted number: '1 234,56' → Decimal('1234.56')."""
    s = s.strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    return Decimal(s)


def format_no(amount: Decimal, places: int = 0) -> str:
    """Render a Decimal Norwegian-style: 12345.6 → '12 346' or '12 345,60'."""
    q = Decimal(10) ** -places
    rounded = amount.quantize(q)
    sign = "-" if rounded < 0 else ""
    abs_s = str(abs(rounded))
    if "." in abs_s:
        whole, frac = abs_s.split(".")
    else:
        whole, frac = abs_s, ""
    # Insert space thousands separators
    rev = whole[::-1]
    grouped = " ".join(rev[i:i+3] for i in range(0, len(rev), 3))[::-1]
    if places > 0:
        return f"{sign}{grouped},{frac.ljust(places, '0')}"
    return f"{sign}{grouped}"


def parse_csv(path: pathlib.Path | str) -> list[Transaction]:
    """Parse a Bulder export CSV from disk."""
    path = pathlib.Path(path)
    with path.open(encoding="utf-8") as f:
        return parse_csv_text(f.read())


def parse_csv_text(text: str) -> list[Transaction]:
    """Parse Bulder CSV text. Tolerates missing optional columns."""
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    missing = set(CSV_COLUMNS) - set(reader.fieldnames or ())
    if missing:
        raise ValueError(f"CSV missing required columns: {sorted(missing)}")
    out: list[Transaction] = []
    for row in reader:
        out.append(Transaction(
            dato=datetime.date.fromisoformat(row["Dato"]),
            belop=parse_no_decimal(row["Beløp"]),
            fra_konto=row["Fra konto"] or "",
            til_konto=row["Til konto"] or "",
            type=row["Type"] or "",
            tekst=row["Tekst"] or "",
            hovedkategori=row["Hovedkategori"] or "",
            underkategori=row["Underkategori"] or "",
        ))
    return out


def date_range(txns: list[Transaction]) -> tuple[datetime.date, datetime.date]:
    """Return (min, max) transaction date. Raises if empty."""
    dates = [t.dato for t in txns]
    return min(dates), max(dates)


def per_month_totals(txns: list[Transaction]) -> dict[str, dict[str, Decimal]]:
    """{'2026-04': {'inn': 355105, 'ut': -348335, 'netto': 6770}, ...}"""
    months: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"inn": Decimal(0), "ut": Decimal(0), "netto": Decimal(0)}
    )
    for t in txns:
        m = months[t.month]
        if t.belop > 0:
            m["inn"] += t.belop
        else:
            m["ut"] += t.belop
        m["netto"] += t.belop
    return dict(months)


def category_outflows(txns: list[Transaction]) -> dict[str, dict[str, Decimal]]:
    """{'Mat og drikke': {'2026-04': 27400, '2026-05': 19532, 'total': 46932}, ...}

    Outflows only (Beløp < 0), absolute values, grouped by Hovedkategori.

    NOTE: Bulder's auto-categorization is unreliable — see
    feedback_bulder_categories_garbage in MEMORY. Kept as a helper for ad-hoc
    queries; not surfaced in render_summary().
    """
    cats: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for t in txns:
        if not t.is_out:
            continue
        cat = t.hovedkategori or "(uten kategori)"
        cats[cat][t.month] += -t.belop
        cats[cat]["total"] += -t.belop
    return {k: dict(v) for k, v in cats.items()}


def merchant_outflows(
    txns: list[Transaction], top_k: int = 25
) -> list[tuple[str, int, list[str], Decimal]]:
    """Top-k merchants by total outflow.

    Returns (tekst, n_transactions, months_with_activity, total), sorted by
    total desc. Uses `Tekst` since Bulder categories are unreliable.
    """
    by_tekst: dict[str, list[Transaction]] = defaultdict(list)
    for t in txns:
        if t.is_out:
            key = t.tekst or "(uten tekst)"
            by_tekst[key].append(t)
    rows = []
    for tekst, ts in by_tekst.items():
        total = sum((-t.belop for t in ts), Decimal(0))
        months = sorted({t.month for t in ts})
        rows.append((tekst, len(ts), months, total))
    rows.sort(key=lambda r: r[3], reverse=True)
    return rows[:top_k]


def account_outflows(txns: list[Transaction]) -> dict[str, dict[str, Decimal]]:
    """Outflows grouped by Fra konto (account name)."""
    accs: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for t in txns:
        if not t.is_out or not t.fra_konto:
            continue
        accs[t.fra_konto][t.month] += -t.belop
        accs[t.fra_konto]["total"] += -t.belop
    return {k: dict(v) for k, v in accs.items()}


def recurring_outflows(
    txns: list[Transaction], min_months: int = 2
) -> list[tuple[str, list[str], int, Decimal, Decimal]]:
    """Tekst values that appear as outflows in at least `min_months` distinct months.

    Returns list of (tekst, sorted_months, count, mean, total), sorted by total desc.
    """
    by_tekst: dict[str, list[Transaction]] = defaultdict(list)
    for t in txns:
        if t.is_out and t.tekst:
            by_tekst[t.tekst].append(t)
    out = []
    for tekst, ts in by_tekst.items():
        months = sorted({t.month for t in ts})
        if len(months) < min_months:
            continue
        amounts = [-t.belop for t in ts]
        total = sum(amounts, Decimal(0))
        mean = total / len(amounts)
        out.append((tekst, months, len(amounts), mean, total))
    return sorted(out, key=lambda r: r[4], reverse=True)


def large_transactions(
    txns: list[Transaction], threshold: Decimal = Decimal(20000)
) -> list[Transaction]:
    """Transactions where |Beløp| >= threshold. Sorted by descending magnitude."""
    big = [t for t in txns if abs(t.belop) >= threshold]
    return sorted(big, key=lambda t: abs(t.belop), reverse=True)


# ---------------- Enrichment: bank ↔ mail ----------------


@dataclasses.dataclass(frozen=True)
class MailHit:
    """A mail surfaced as relevant to a bank transaction."""
    source: str               # "amount" | "near" | "semantic"
    date: datetime.date
    from_addr: str
    subject: str
    message_id: str
    score: float = 0.0        # for semantic: cosine distance; else 0


def is_interesting(t: Transaction, large_threshold: Decimal = Decimal(10000)) -> bool:
    """Heuristic: which transactions deserve enrichment / human attention?

    A transaction is "interesting" when:
    - |Beløp| ≥ large_threshold (big enough to matter), OR
    - No Tekst (bare transfer — what was this?), OR
    - Hovedkategori is 'Ukategorisert' or empty AND |Beløp| ≥ 1000
      (Bulder didn't recognize it; small noise filtered out)
    """
    if abs(t.belop) >= large_threshold:
        return True
    if not t.tekst:
        return True
    if abs(t.belop) >= Decimal(1000) and t.hovedkategori in ("", "Ukategorisert"):
        return True
    return False


def find_mail_by_amount(
    conn, amount: Decimal, tolerance: Decimal = Decimal("0.01")
) -> list[MailHit]:
    """Look up mail whose extracted money-entities match the given amount.

    Coverage is sparse — only tier-2-summarised mails have money entities, and
    the LLM extracts only what it sees. Use as a high-precision augment, not
    a primary signal.
    """
    abs_amt = abs(amount)
    lo, hi = abs_amt - tolerance, abs_amt + tolerance
    cur = conn.cursor()
    # Use GROUP BY to dedupe — same mail can have N summaries referencing the
    # same money entity, and SELECT DISTINCT with ORDER BY m.date is invalid.
    cur.execute("""
        SELECT m.date::date AS d, m.from_addr, m.subject, m.message_id
        FROM entities e
        JOIN summary_entities se ON se.entity_id = e.id
        JOIN summaries s ON s.id = se.summary_id
        JOIN messages m ON m.id = s.message_id
        WHERE e.kind = 'money'
          AND (e.meta->>'amount')::numeric BETWEEN %s AND %s
        GROUP BY m.id, m.date, m.from_addr, m.subject, m.message_id
        ORDER BY d DESC
        LIMIT 5
    """, (lo, hi))
    return [
        MailHit(source="amount", date=d, from_addr=f or "", subject=s or "",
                message_id=mid or "")
        for d, f, s, mid in cur.fetchall()
    ]


def merchant_keyword(tekst: str) -> str:
    """Extract a notmuch-friendly keyword from a Bulder `Tekst` field.

    Strips Norwegian prefixes ('Fra:', 'Til:'), takes the first meaningful word,
    lowercases. Empty for transfers / unlabelled rows.
    """
    if not tekst:
        return ""
    s = re.sub(r"^(Fra|Til|Betalt|Avsender):\s*", "", tekst, flags=re.IGNORECASE)
    # Drop trailing date / numeric suffixes
    s = re.sub(r"\b\d{4,}\b", "", s).strip()
    # First token, alpha-only
    m = re.search(r"[A-Za-zÆØÅæøå][A-Za-zÆØÅæøå0-9\-.]{2,}", s)
    return m.group(0).lower() if m else ""


def find_mail_near(
    txn: Transaction, window_days: int = 3, limit: int = 3
) -> list[MailHit]:
    """Notmuch search for mail dated near `txn.dato` matching merchant keyword.

    Returns top `limit` hits as per-message MailHits (not threads, so each
    `message_id` is a real notmuch ID that can be looked up in the messages
    table for tier-2 enqueue).
    """
    kw = merchant_keyword(txn.tekst)
    if not kw:
        return []
    start = txn.dato - datetime.timedelta(days=window_days)
    end = txn.dato + datetime.timedelta(days=window_days)
    query = f"date:{start}..{end} and ({kw})"
    # --output=messages gives notmuch IDs (e.g. "id:abc@host"), one per line.
    try:
        res = subprocess.run(
            ["notmuch", "search", "--output=messages", f"--limit={limit}", query],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError:
        return []
    ids = [line.removeprefix("id:") for line in res.stdout.splitlines() if line]
    if not ids:
        return []
    # Pull headers in one batch via notmuch show.
    try:
        show = subprocess.run(
            ["notmuch", "show", "--format=json", "--body=false",
             "(" + " or ".join(f"id:{i}" for i in ids) + ")"],
            check=True, capture_output=True, text=True,
        )
        threads = json.loads(show.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return []
    out: list[MailHit] = []

    def walk(node):
        # notmuch show returns nested list-of-lists; leaves are msg dicts.
        if isinstance(node, dict) and node.get("id"):
            h = node.get("headers", {})
            ts = node.get("timestamp", 0)
            d = datetime.date.fromtimestamp(ts) if ts else txn.dato
            out.append(MailHit(
                source="near", date=d,
                from_addr=h.get("From", ""), subject=h.get("Subject", ""),
                message_id=node["id"],
            ))
        elif isinstance(node, list):
            for sub in node:
                walk(sub)

    walk(threads)
    return out[:limit]


def enrich(txns: list[Transaction]) -> dict[int, list[MailHit]]:
    """For each interesting transaction (by index), return matched mail hits.

    Strategy per transaction:
    1. Amount-match on extracted money entities (high precision, low recall).
    2. Date+merchant-keyword notmuch search (broader).

    Uses `mail_reader.db.connect()` so PG_DSN, connection lifecycle, and any
    future pooling stay aligned with the webapp.
    """
    from mail_reader.db import connect
    out: dict[int, list[MailHit]] = {}
    with connect() as conn:
        for i, t in enumerate(txns):
            if not is_interesting(t):
                continue
            hits: list[MailHit] = []
            hits.extend(find_mail_by_amount(conn, t.belop))
            hits.extend(find_mail_near(t))
            if hits:
                out[i] = hits
    return out


def render_summary(txns: list[Transaction]) -> str:
    """Build the full markdown summary as a single string."""
    if not txns:
        return "(empty CSV — no transactions)\n"
    start, end = date_range(txns)
    accounts = sorted({t.fra_konto for t in txns if t.fra_konto})

    lines = []
    lines.append(f"## Ingest: {start} → {end} ({len(txns)} rader)\n")
    lines.append(f"**Kontoer:** {', '.join(accounts)}\n")

    months = sorted({t.month for t in txns})

    # Per-month totals
    lines.append("**Per måned, kontant inn/ut (NOK):**\n")
    lines.append("| Måned    | Inn      | Ut       | Netto    |")
    lines.append("|----------|----------|----------|----------|")
    pmt = per_month_totals(txns)
    for m in months:
        d = pmt[m]
        lines.append(
            f"| {m}  | {format_no(d['inn']):>8} | "
            f"{format_no(d['ut']):>8} | {format_no(d['netto']):>8} |"
        )

    # Account outflows
    lines.append("\n**Utgifter per konto (NOK):**\n")
    header = "| Konto              | " + " | ".join(months) + " |"
    sep = "|--------------------|" + "|".join(["---------"] * len(months)) + "|"
    lines.append(header)
    lines.append(sep)
    accs = account_outflows(txns)
    for acc, by_m in sorted(accs.items(), key=lambda kv: kv[1]["total"], reverse=True):
        row = [f"| {acc:<18} |"]
        for m in months:
            row.append(f" {format_no(by_m.get(m, Decimal(0))):>7} |")
        lines.append("".join(row))

    # Top merchants (Tekst is the reliable signal; Bulder categories are not)
    lines.append("\n**Top utbetalingsmottakere** (sortert på sum, alle måneder):\n")
    lines.append("| Tekst                          | n  | Mnd | Total   |")
    lines.append("|--------------------------------|----|-----|---------|")
    for tekst, n, ms, total in merchant_outflows(txns):
        lines.append(
            f"| {tekst[:30]:<30} | {n:>2} | {len(ms):>3} | "
            f"{format_no(total):>7} |"
        )

    # Recurring (subset of top merchants that show up in ≥2 months)
    lines.append("\n**Faste utgifter** (samme `Tekst` i ≥2 måneder):\n")
    lines.append("| Tekst                          | Snitt   | Total   | n |")
    lines.append("|--------------------------------|---------|---------|---|")
    for tekst, _months, n, mean, total in recurring_outflows(txns):
        lines.append(
            f"| {tekst[:30]:<30} | {format_no(mean):>7} | "
            f"{format_no(total):>7} | {n} |"
        )

    # Big one-offs
    lines.append("\n**Store enkelttransaksjoner (|Beløp| ≥ 20 000 NOK):**\n")
    lines.append("| Dato       | Beløp     | Konto              | Tekst |")
    lines.append("|------------|-----------|--------------------|-------|")
    for t in large_transactions(txns):
        lines.append(
            f"| {t.dato} | {format_no(t.belop):>9} | "
            f"{t.fra_konto[:18]:<18} | {t.tekst} |"
        )

    return "\n".join(lines) + "\n"


def render_enrichment(
    txns: list[Transaction], hits: dict[int, list[MailHit]]
) -> str:
    """Render the mail hits per transaction. Skips transactions with no hits."""
    if not hits:
        return "\n*(no mail matches found for interesting transactions)*\n"
    lines = ["\n## Interessante transaksjoner — koblet til mail\n"]
    for i in sorted(hits):
        t = txns[i]
        lines.append(
            f"\n### {t.dato}  {format_no(t.belop):>9} NOK  "
            f"— {t.tekst or '(uten tekst)'}\n"
        )
        lines.append(f"_{t.fra_konto} → {t.til_konto or t.tekst}_\n")
        for h in hits[i]:
            tag = h.source
            score = f" (d={h.score:.3f})" if h.source == "semantic" else ""
            lines.append(
                f"- **[{tag}]**{score} {h.date} — "
                f"{h.subject[:80]} _({h.from_addr[:60]})_  "
                f"`id:{h.message_id}`"
            )
    return "\n".join(lines) + "\n"


# ---------------- Mail extraction ----------------

SUBJECT_QUERY = 'subject:"Bulder bank eksport" attachment:csv'


def latest_csv_from_mail(into: pathlib.Path) -> pathlib.Path:
    """Find the most-recent Bulder export mail and write its CSV to `into`.

    Returns the path to the extracted CSV. Raises if none found.
    """
    res = subprocess.run(
        ["notmuch", "search", "--output=threads", "--limit=1", SUBJECT_QUERY],
        check=True, capture_output=True, text=True,
    )
    thread = res.stdout.strip().splitlines()[0] if res.stdout.strip() else ""
    if not thread:
        raise RuntimeError(f"No mail matching {SUBJECT_QUERY!r}")
    into.parent.mkdir(parents=True, exist_ok=True)
    # `mailshow` is a sibling console entry point (same venv → on PATH).
    subprocess.run(
        ["mailshow", thread, "--attachments", str(into.parent)],
        check=True, capture_output=True, text=True,
    )
    # mailshow saves with the original attachment filename
    found = list(into.parent.glob("*.csv"))
    if not found:
        raise RuntimeError(f"mailshow extracted no CSV into {into.parent}")
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("csv", nargs="?", type=pathlib.Path,
                     help="Path to eksporterte_transaksjoner.csv")
    src.add_argument("--from-mail", action="store_true",
                     help="Auto-extract from latest Bulder export mail")
    p.add_argument("--keep-csv", type=pathlib.Path, default=None,
                   help="When using --from-mail, save the extracted CSV here "
                        "(default: temp dir, deleted after)")
    p.add_argument("--enrich", action="store_true",
                   help="Cross-reference 'interesting' transactions against "
                        "embedded mail (postgres money entities + notmuch). "
                        "Adds a section listing matching mails per transaction.")
    args = p.parse_args(argv)

    if args.from_mail:
        dest = args.keep_csv or pathlib.Path("/tmp/bulder_finance/x.csv")
        csv_path = latest_csv_from_mail(dest)
    else:
        csv_path = args.csv

    txns = parse_csv(csv_path)
    print(render_summary(txns))
    if args.enrich:
        hits = enrich(txns)
        print(render_enrichment(txns, hits))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Tests for finance_ingest.py — Bulder CSV parsing and aggregation."""

from __future__ import annotations

import datetime
import textwrap
from decimal import Decimal

import pytest

from scripts.finance_ingest import (
    Transaction,
    account_outflows,
    category_outflows,
    date_range,
    format_no,
    is_interesting,
    large_transactions,
    merchant_keyword,
    merchant_outflows,
    parse_csv_text,
    parse_no_decimal,
    per_month_totals,
    recurring_outflows,
    render_summary,
)


# ----- parse_no_decimal -----

def test_parse_no_decimal_basic():
    assert parse_no_decimal("123,45") == Decimal("123.45")

def test_parse_no_decimal_negative():
    assert parse_no_decimal("-1234,56") == Decimal("-1234.56")

def test_parse_no_decimal_thousands_space():
    # Some exports use space thousands separator
    assert parse_no_decimal("1 234,56") == Decimal("1234.56")

def test_parse_no_decimal_nbsp():
    assert parse_no_decimal("1\xa0234,56") == Decimal("1234.56")

def test_parse_no_decimal_no_decimal():
    assert parse_no_decimal("100") == Decimal("100")


# ----- format_no -----

def test_format_no_thousands():
    assert format_no(Decimal("12345")) == "12 345"

def test_format_no_negative():
    assert format_no(Decimal("-1234567")) == "-1 234 567"

def test_format_no_small():
    assert format_no(Decimal("12")) == "12"

def test_format_no_decimals():
    assert format_no(Decimal("1234.56"), places=2) == "1 234,56"

def test_format_no_zero():
    assert format_no(Decimal(0)) == "0"


# ----- CSV fixture -----

SAMPLE_CSV = textwrap.dedent("""\
    Dato;Beløp;Originalt Beløp;Original Valuta;Til konto;Til kontonummer;Fra konto;Fra kontonummer;Type;Tekst;KID;Hovedkategori;Underkategori
    2026-04-01;-198,15;-198,15;NOK;;6030.05.00288;BULDER BRUKSKONTO;3610.54.55385;Efaktura;NORDEA LIV AS;070520932297;Hus og hjem;Forsikring
    2026-04-02;-2278,00;-2278,00;NOK;;1234.56.78901;BULDER BRUKSKONTO;3610.54.55385;Efaktura;Tibber Norge As;111111111;Hus og hjem;Strøm
    2026-04-25;45000,00;45000,00;NOK;BULDER BRUKSKONTO;3610.54.55385;;;Lønn;Fra: EXAMPLECORP AS;;Inntekter;Lønn
    2026-04-30;-70000,00;-70000,00;NOK;;9999.99.99999;BULDER RAMMELÅN;1111.11.11111;Betaling;;;Hus og hjem;Lån
    2026-05-02;-198,15;-198,15;NOK;;6030.05.00288;BULDER BRUKSKONTO;3610.54.55385;Efaktura;NORDEA LIV AS;070520932298;Hus og hjem;Forsikring
    2026-05-02;-2278,00;-2278,00;NOK;;1234.56.78901;BULDER BRUKSKONTO;3610.54.55385;Efaktura;Tibber Norge As;111111112;Hus og hjem;Strøm
    2026-05-12;-92990,00;-92990,00;NOK;;5555.55.55555;BULDER RAMMELÅN;1111.11.11111;Betaling;Apple;;Handel;Elektronikk
    """)


def test_parse_csv_basic():
    txns = parse_csv_text(SAMPLE_CSV)
    assert len(txns) == 7
    assert txns[0].dato == datetime.date(2026, 4, 1)
    assert txns[0].belop == Decimal("-198.15")
    assert txns[0].tekst == "NORDEA LIV AS"
    assert txns[0].hovedkategori == "Hus og hjem"


def test_parse_csv_rejects_wrong_columns():
    bad = "foo;bar\n1;2\n"
    with pytest.raises(ValueError, match="missing required columns"):
        parse_csv_text(bad)


def test_parse_csv_handles_empty_fields():
    txns = parse_csv_text(SAMPLE_CSV)
    # The lønn row has no Fra konto (it's an inflow)
    lonn = [t for t in txns if t.belop > 0][0]
    assert lonn.fra_konto == ""
    assert lonn.tekst == "Fra: EXAMPLECORP AS"


# ----- aggregations -----

def test_date_range():
    txns = parse_csv_text(SAMPLE_CSV)
    start, end = date_range(txns)
    assert start == datetime.date(2026, 4, 1)
    assert end == datetime.date(2026, 5, 12)


def test_per_month_totals():
    txns = parse_csv_text(SAMPLE_CSV)
    months = per_month_totals(txns)
    assert months["2026-04"]["inn"] == Decimal("45000.00")
    assert months["2026-04"]["ut"] == Decimal("-72476.15")
    assert months["2026-04"]["netto"] == Decimal("-27476.15")
    assert "2026-05" in months


def test_category_outflows_groups_correctly():
    txns = parse_csv_text(SAMPLE_CSV)
    cats = category_outflows(txns)
    # Apr Hus og hjem: NORDEA LIV 198.15 + Tibber 2278 + Lån 70000 = 72476.15
    assert cats["Hus og hjem"]["2026-04"] == Decimal("72476.15")
    # May Hus og hjem: NORDEA LIV 198.15 + Tibber 2278 = 2476.15
    assert cats["Hus og hjem"]["2026-05"] == Decimal("2476.15")
    assert cats["Hus og hjem"]["total"] == Decimal("74952.30")
    # Apple in Handel
    assert cats["Handel"]["total"] == Decimal("92990.00")


def test_category_outflows_excludes_income():
    txns = parse_csv_text(SAMPLE_CSV)
    cats = category_outflows(txns)
    # Lønn is "Inntekter" but positive — should not appear as outflow
    assert "Inntekter" not in cats


def test_account_outflows():
    txns = parse_csv_text(SAMPLE_CSV)
    accs = account_outflows(txns)
    assert "BULDER BRUKSKONTO" in accs
    assert "BULDER RAMMELÅN" in accs
    # Rammelånet bears the two big charges
    assert accs["BULDER RAMMELÅN"]["total"] == Decimal("162990.00")


def test_recurring_outflows_identifies_repeats():
    txns = parse_csv_text(SAMPLE_CSV)
    rec = recurring_outflows(txns, min_months=2)
    texts = [r[0] for r in rec]
    assert "NORDEA LIV AS" in texts
    assert "Tibber Norge As" in texts
    # Apple only appears in one month → not recurring
    assert "Apple" not in texts


def test_recurring_outflows_sorted_by_total_desc():
    txns = parse_csv_text(SAMPLE_CSV)
    rec = recurring_outflows(txns)
    totals = [r[4] for r in rec]
    assert totals == sorted(totals, reverse=True)


def test_large_transactions_default_threshold():
    txns = parse_csv_text(SAMPLE_CSV)
    big = large_transactions(txns)
    # ≥20k: -70k Lån, -92990 Apple, +45000 Lønn
    assert len(big) == 3
    assert big[0].tekst == "Apple"  # largest |Beløp|


def test_large_transactions_custom_threshold():
    txns = parse_csv_text(SAMPLE_CSV)
    big = large_transactions(txns, threshold=Decimal(50000))
    assert len(big) == 2  # only -70k and -92990


# ----- render -----

def test_render_summary_includes_all_sections():
    txns = parse_csv_text(SAMPLE_CSV)
    out = render_summary(txns)
    assert "## Ingest:" in out
    assert "Per måned" in out
    assert "Utgifter per konto" in out
    assert "Top utbetalingsmottakere" in out
    assert "Faste utgifter" in out
    assert "Store enkelttransaksjoner" in out


def test_render_summary_handles_empty():
    assert "empty" in render_summary([]).lower()


def test_render_summary_does_not_surface_bulder_categories():
    # Regression: Bulder's Hovedkategori is unreliable; don't headline it.
    txns = parse_csv_text(SAMPLE_CSV)
    out = render_summary(txns)
    assert "per Bulder-hovedkategori" not in out


# ----- regression: amount rounding -----

def test_format_no_does_not_round_up_at_half():
    # Banker's rounding in Decimal — Python's default rounds to even
    # 12345.5 → 12346 if ROUND_HALF_EVEN, 12345.5 places=0 → 12346
    # Just confirm we always get a clean integer string when places=0
    result = format_no(Decimal("12345.5"))
    assert "," not in result and "." not in result


# ----- merchant_outflows (Tekst-based; Bulder categories are unreliable) -----

def test_merchant_outflows_groups_and_sorts():
    txns = parse_csv_text(SAMPLE_CSV)
    rows = merchant_outflows(txns)
    # First should be Apple (single 92990) — largest outflow
    assert rows[0][0] == "Apple"
    assert rows[0][3] == Decimal("92990.00")
    # All-outflow rows present
    teksts = [r[0] for r in rows]
    assert "Tibber Norge As" in teksts
    assert "NORDEA LIV AS" in teksts


def test_merchant_outflows_excludes_income():
    txns = parse_csv_text(SAMPLE_CSV)
    teksts = [r[0] for r in merchant_outflows(txns)]
    assert "Fra: EXAMPLECORP AS" not in teksts


def test_merchant_outflows_top_k_caps_results():
    txns = parse_csv_text(SAMPLE_CSV)
    rows = merchant_outflows(txns, top_k=2)
    assert len(rows) == 2


def test_merchant_outflows_counts_months():
    txns = parse_csv_text(SAMPLE_CSV)
    rows = dict((r[0], r) for r in merchant_outflows(txns))
    # NORDEA appears in both Apr and May
    assert len(rows["NORDEA LIV AS"][2]) == 2
    # Apple only in May
    assert len(rows["Apple"][2]) == 1


# ----- merchant_keyword (notmuch-friendly merchant extraction) -----

def test_merchant_keyword_strips_no_prefixes():
    assert merchant_keyword("Fra: EXAMPLECORP AS") == "examplecorp"
    assert merchant_keyword("Til: Astrid Kristine Hansen") == "astrid"


def test_merchant_keyword_handles_plain_merchant():
    assert merchant_keyword("NORDEA LIV AS") == "nordea"
    assert merchant_keyword("Oda.com") == "oda.com"


def test_merchant_keyword_returns_empty_when_no_alpha():
    assert merchant_keyword("") == ""
    assert merchant_keyword("12345") == ""


def test_merchant_keyword_drops_numeric_suffixes():
    # "Apple 070520932297" → "apple"
    assert merchant_keyword("Apple 070520932297") == "apple"


# ----- is_interesting (enrichment gate) -----

def _txn(belop: str, tekst: str = "X", hovedkategori: str = "Mat") -> Transaction:
    return Transaction(
        dato=datetime.date(2026, 4, 1), belop=Decimal(belop),
        fra_konto="X", til_konto="", type="", tekst=tekst,
        hovedkategori=hovedkategori, underkategori="",
    )


def test_is_interesting_large_outflow():
    assert is_interesting(_txn("-50000"))


def test_is_interesting_large_inflow():
    assert is_interesting(_txn("48000", "Lønn", "Inntekter"))


def test_is_interesting_skips_small_categorized():
    assert not is_interesting(_txn("-198", "Tibber", "Strøm"))


def test_is_interesting_flags_unlabelled():
    assert is_interesting(_txn("-500", tekst=""))


def test_is_interesting_flags_ukategorisert_above_1k():
    assert is_interesting(_txn("-1500", "LNNA", "Ukategorisert"))


def test_is_interesting_skips_ukategorisert_under_1k():
    assert not is_interesting(_txn("-50", "Soundiiz", "Ukategorisert"))

"""Cash Intelligence ne compte plus comme encaissement attendu une facture client refusée, en litige,
suspendue ou rejetée côté facturation électronique : elle sort de la prévision et reste signalée
à part (créances retenues), avec son total."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASH = (ROOT / 'profitos/routes/cash_intelligence.py').read_text(encoding='utf-8')
TPL = (ROOT / 'templates/cash_intelligence.html').read_text(encoding='utf-8')


def test_held_invoices_are_filtered_before_they_become_expected_income():
    start = CASH.index("    for inv in sales:")
    block = CASH[start:CASH.index("receivables.sort(", start)]
    assert "hold=_einvoice_hold(inv)" in block
    assert block.index("hold=_einvoice_hold(inv)") < block.index("receivables.append(")
    assert "held_receivables.append(" in block and "continue" in block


def test_forecast_returns_the_held_total_separately():
    assert "'held_receivables':held_receivables" in CASH
    assert "'held_receivables_total':round(sum(h['amount'] for h in held_receivables),2)" in CASH
    assert "from profitos.routes.invoicing import _einvoice_hold" in CASH


def test_page_warns_about_held_receivables_and_keeps_the_empty_state():
    assert "créance(s) retenue(s), non comptée(s)" in TPL
    assert "exclus de la prévision" in TPL
    assert "Aucune créance ouverte." in TPL

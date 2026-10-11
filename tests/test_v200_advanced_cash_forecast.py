from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
CASH=(ROOT/'profitos/routes/cash_intelligence.py').read_text(encoding='utf-8')
TPL=(ROOT/'templates/cash_intelligence.html').read_text(encoding='utf-8')

def test_forecast_uses_real_sales_and_payment_ledger():
    assert 'FROM outgoing_invoices oi' in CASH
    assert 'outgoing_invoice_payments' in CASH
    assert "oi.entity_id IS ?" in CASH
    assert "amount=max(0.0,round(_safe_float(inv['total'])-_safe_float(inv['paid_total']),2))" in CASH

def test_forecast_uses_supplier_open_balance_and_payment_ledger():
    assert 'FROM purchase_invoices pi' in CASH
    assert 'purchase_invoice_payments' in CASH
    assert "pi.entity_id IS ?" in CASH
    assert "'supplier_payables':supplier_payables" in CASH

def test_future_expenses_are_deduplicated_against_supplier_invoices():
    assert 'purchase_signatures' in CASH
    assert 'if sig in purchase_signatures: continue' in CASH

def test_scenarios_do_not_write_accounting():
    assert '_simulate_curve' in CASH
    assert 'generate_sale' not in CASH
    assert 'generate_purchase' not in CASH
    assert 'INSERT INTO accounting_' not in CASH

def test_ui_exposes_supplier_outflows_and_partial_payment_semantics():
    assert 'DÉCAISSEMENTS FOURNISSEURS' in TPL
    assert 'paiement partiel déjà déduit' in TPL
    assert 'cash.supplier_payables' in TPL

from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=(ROOT/'profitos/runtime.py').read_text(encoding='utf-8')
ROUTES=(ROOT/'profitos/routes/invoicing.py').read_text(encoding='utf-8')
ACCOUNTING=(ROOT/'profitos/accounting.py').read_text(encoding='utf-8')
DETAIL=(ROOT/'templates/invoicing_detail.html').read_text(encoding='utf-8')
LIST=(ROOT/'templates/invoicing_list.html').read_text(encoding='utf-8')
PUBLIC=(ROOT/'templates/invoicing_public.html').read_text(encoding='utf-8')

def test_payment_ledger_schema_is_entity_scoped_and_idempotent():
    assert 'CREATE TABLE IF NOT EXISTS outgoing_invoice_payments' in RUNTIME
    assert 'entity_id INTEGER' in RUNTIME
    assert 'invoice_id INTEGER NOT NULL' in RUNTIME
    assert 'UNIQUE(entity_id, idempotency_key)' in RUNTIME
    assert 'idx_outgoing_invoice_payments_invoice' in RUNTIME

def test_partial_payment_accounting_uses_payment_as_source_and_entity():
    assert 'def generate_sale_partial_payment_entry' in ACCOUNTING
    assert "source_type = 'outgoing_invoice_payment_v2'" in ACCOUNTING
    assert "source_id=payment['id']" in ACCOUNTING
    assert 'entity_id=entity_id' in ACCOUNTING
    assert "{'account_code': '512000', 'debit': amount}" in ACCOUNTING
    assert "{'account_code': '411000', 'credit': amount" in ACCOUNTING

def test_payment_route_computes_balance_and_partial_status_atomically():
    assert "@app.route('/facturation/<int:invoice_id>/reglement',methods=['POST'])" in ROUTES
    assert "balance=_invoice_balance(c,inv)" in ROUTES
    assert "amount > balance + 0.001" in ROUTES
    assert "generate_sale_partial_payment_entry(c,inv,payment)" in ROUTES
    assert "new_status='paid' if paid_total" in ROUTES
    assert "else 'partially_paid'" in ROUTES
    assert 'except AccountingError as e:' in ROUTES
    assert 'c.rollback()' in ROUTES

def test_receivables_are_entity_scoped_and_net_of_payments():
    block=ROUTES[ROUTES.index('def invoicing_receivables():'):ROUTES.index("@app.route('/facturation/<int:invoice_id>/", ROUTES.index('def invoicing_receivables():'))]
    assert "i.status IN ('sent','partially_paid') AND i.entity_id IS ?" in block
    assert 'p.entity_id IS i.entity_id' in block
    assert "total-credited-float(r['paid_total'] or 0)" in block

def test_invoice_ui_exposes_payment_history_and_partial_status():
    assert 'Règlements' in DETAIL
    assert 'paid_total' in DETAIL and 'balance_due' in DETAIL
    assert "url_for('invoicing_add_payment'" in DETAIL
    assert 'Partiellement payée' in DETAIL
    assert 'Partiellement payée' in LIST
    assert 'Partiellement réglée' in PUBLIC

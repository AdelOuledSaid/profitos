from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INV = (ROOT / 'profitos/routes/invoicing.py').read_text(encoding='utf-8')
RUNTIME = (ROOT / 'profitos/runtime.py').read_text(encoding='utf-8')
WEI = (ROOT / 'profitos/weinvoice.py').read_text(encoding='utf-8')


def test_customer_invoices_are_scoped_to_current_entity():
    assert "SELECT * FROM outgoing_invoices WHERE entity_id IS ? ORDER BY id DESC" in INV
    assert "SELECT * FROM outgoing_invoices WHERE id=? AND entity_id IS ?" in INV
    assert "def _current_invoice" in INV


def test_credit_notes_are_scoped_and_numbered_per_entity():
    assert "SELECT * FROM outgoing_credit_notes WHERE id=? AND entity_id IS ?" in INV
    assert "original_invoice_id=? AND entity_id IS ? AND status='issued'" in INV
    assert 'f"AV-E{entity_id}-{year}-"' in INV
    assert '(entity_id,credit_number,original_invoice_id' in INV


def test_mark_paid_is_idempotent_and_entity_scoped():
    assert "if inv['status']=='paid':" in INV
    assert "AND entity_id IS ? AND status!='paid'" in INV
    assert 'if cur.rowcount != 1:' in INV


def test_credit_creation_serializes_remaining_balance_check():
    assert "c.execute('BEGIN IMMEDIATE')" in INV
    assert "already=_credited_total(c,invoice_id,inv['entity_id'])" in INV


def test_weinvoice_submission_uses_stable_idempotency_and_entity_scope():
    assert "f\"profitos-{session.get('org_id')}-{invoice_id}\"" in INV
    assert "weinvoice_idempotency_key=?,weinvoice_last_error=NULL WHERE id=? AND entity_id IS ?" in INV
    assert "weinvoice_invoice_id=?,weinvoice_status=?,weinvoice_sent_at=?,weinvoice_last_error=NULL WHERE id=? AND entity_id IS ?" in INV


def test_weinvoice_webhook_is_deduplicated():
    assert 'CREATE TABLE IF NOT EXISTS weinvoice_webhook_events' in WEI
    assert "SELECT 1 FROM weinvoice_webhook_events WHERE event_id=?" in WEI
    assert "INSERT INTO weinvoice_webhook_events" in WEI


def test_new_and_legacy_schema_have_invoice_and_credit_entity_id():
    invoice_block = RUNTIME[RUNTIME.index('CREATE TABLE IF NOT EXISTS outgoing_invoices'):RUNTIME.index('CREATE TABLE IF NOT EXISTS outgoing_quotes')]
    credit_block = RUNTIME[RUNTIME.index('CREATE TABLE IF NOT EXISTS outgoing_credit_notes'):RUNTIME.index('CREATE TABLE IF NOT EXISTS accounting_chart_of_accounts')]
    assert 'entity_id INTEGER' in invoice_block
    assert 'entity_id INTEGER' in credit_block
    assert "('outgoing_invoices','entity_id')" in RUNTIME
    assert "('outgoing_credit_notes','entity_id')" in RUNTIME


def test_public_invoice_uses_owning_entity_identity():
    public = INV[INV.index("@app.route('/facture/<token>')"):]
    assert "company_row=resolve_entity(tc,inv['entity_id'])" in public

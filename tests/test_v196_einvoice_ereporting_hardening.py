from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
INV=(ROOT/'profitos/routes/invoicing.py').read_text(encoding='utf-8')
RT=(ROOT/'profitos/runtime.py').read_text(encoding='utf-8')
WE=(ROOT/'profitos/weinvoice.py').read_text(encoding='utf-8')
TPL=(ROOT/'templates/invoicing_detail.html').read_text(encoding='utf-8')


def test_einvoice_event_ledger_is_entity_scoped_and_append_only():
    assert 'CREATE TABLE IF NOT EXISTS einvoice_events' in RT
    assert 'entity_id INTEGER' in RT
    assert 'idx_einvoice_events_invoice' in RT
    assert '_record_einvoice_event' in INV
    assert "'submission_failed'" in INV
    assert "'submitted'" in INV
    assert "'status_sync'" in INV
    assert "'webhook_status'" in WE


def test_webhook_status_update_is_entity_scoped_and_deduplicated():
    assert 'SELECT id,entity_id FROM outgoing_invoices WHERE weinvoice_invoice_id=?' in WE
    assert 'WHERE id=? AND entity_id IS ?' in WE
    assert 'weinvoice_webhook_events' in WE
    assert 'INSERT OR IGNORE INTO einvoice_events' in WE


def test_ereporting_staging_supports_transactions_and_payments():
    assert 'CREATE TABLE IF NOT EXISTS ereporting_records' in RT
    assert "record_type TEXT NOT NULL" in RT
    assert "status TEXT NOT NULL DEFAULT 'pending'" in RT
    assert '_stage_ereporting_record' in INV
    assert "_stage_ereporting_record(c,inv,'transaction')" in INV
    assert '_stage_payment_ereporting' in INV
    assert "'outgoing_invoice_payment'" in INV


def test_invoice_compliance_uses_owning_entity_and_ui_shows_history():
    detail=INV[INV.index('def invoicing_detail(invoice_id):'):INV.index("@app.route('/facturation/<int:invoice_id>/pdf')")]
    assert "resolve_entity(c,inv['entity_id'])" in detail
    assert "SELECT * FROM company WHERE id=1" not in detail
    assert 'Historique de transmission' in TPL
    assert 'plateforme agréée' in TPL

from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=(ROOT/'profitos/runtime.py').read_text(encoding='utf-8')
WEINV=(ROOT/'profitos/weinvoice.py').read_text(encoding='utf-8')
MAIN=(ROOT/'profitos/routes/main.py').read_text(encoding='utf-8')
INV=(ROOT/'profitos/routes/invoicing.py').read_text(encoding='utf-8')


def test_weinvoice_has_entity_scoped_settings_table_and_legacy_parent_seed():
    assert 'CREATE TABLE IF NOT EXISTS weinvoice_entity_settings' in RUNTIME
    assert 'entity_key INTEGER PRIMARY KEY' in RUNTIME
    assert "VALUES(0," in RUNTIME
    assert 'weinvoice_agreements' in RUNTIME and "('weinvoice_agreements','entity_id')" in RUNTIME


def test_invoice_weinvoice_operations_use_invoice_entity_settings():
    assert INV.count('FROM weinvoice_entity_settings WHERE entity_key=?') >= 3
    block=INV[INV.index('def invoicing_send_weinvoice'):INV.index('def invoicing_send(', INV.index('def invoicing_send_weinvoice'))]
    assert 'FROM app_settings WHERE id=1' not in block
    assert "inv['entity_id'] or 0" in block


def test_company_weinvoice_actions_are_current_entity_scoped():
    assert 'weinvoice_entity_settings(c,current_entity_id())' in MAIN
    assert 'entity_id=current_entity_id()' in MAIN
    assert 'company_row=resolve_entity(c,eid)' in MAIN
    assert 'entity_id=eid' in MAIN


def test_weinvoice_storage_and_webhook_are_entity_scoped():
    assert 'def get_entity_settings(conn, entity_id=None)' in WEINV
    assert 'def _store_entity_settings(conn, entity_id=None' in WEINV
    assert 'UPDATE app_settings SET weinvoice_' not in WEINV
    assert 'WHERE weinvoice_company_id=?' in WEINV
    assert 'WEINVOICE_ONBOARDING_WEBHOOK_UNSCOPED' in WEINV

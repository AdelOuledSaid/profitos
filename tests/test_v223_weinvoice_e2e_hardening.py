from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WE = (ROOT / 'profitos' / 'weinvoice.py').read_text(encoding='utf-8')
MAIN = (ROOT / 'profitos' / 'routes' / 'main.py').read_text(encoding='utf-8')
INV = (ROOT / 'profitos' / 'routes' / 'invoicing.py').read_text(encoding='utf-8')
RUNTIME = (ROOT / 'profitos' / 'runtime.py').read_text(encoding='utf-8')


def test_invoice_webhook_has_dedicated_secret_with_legacy_fallback():
    assert 'WEINVOICE_INVOICE_WEBHOOK_SECRET' in RUNTIME
    assert 'WEINVOICE_INVOICE_WEBHOOK_SECRET' in WE
    assert "secret_name='invoice'" in MAIN
    assert 'WEINVOICE_INVOICE_WEBHOOK_SECRET or WEINVOICE_WEBHOOK_SECRET' in WE


def test_signature_verifier_accepts_invoice_secret_selector():
    assert "def verify_webhook_signature(webhook_id, webhook_timestamp, raw_body, signature_header, tolerance_seconds=300, secret_name='management')" in WE
    assert 'webhook_secret' in WE
    assert 'hmac.compare_digest' in WE


def test_weinvoice_sync_checks_invoice_before_dereferencing_entity():
    marker = "def invoicing_sync_weinvoice(invoice_id):"
    block = INV[INV.index(marker):INV.index("def invoicing_test_weinvoice_webhook_sandbox", INV.index(marker))]
    assert block.index('if not inv: abort(404)') < block.index("inv['entity_id']")


def test_weinvoice_sandbox_test_checks_invoice_before_dereferencing_entity():
    marker = "def invoicing_test_weinvoice_webhook_sandbox(invoice_id):"
    block = INV[INV.index(marker):INV.index("def invoicing_send(invoice_id):", INV.index(marker))]
    assert block.index('if not inv: abort(404)') < block.index("inv['entity_id']")


def test_invoice_webhook_remains_signed_idempotent_and_entity_scoped():
    assert '/webhooks/weinvoice/invoice-status' in MAIN
    assert 'weinvoice_verify_webhook' in MAIN
    assert 'weinvoice_webhook_events' in WE
    assert 'entity_id IS ?' in WE
    assert 'WEINVOICE_INVOICE_STATUS_UPDATED' in WE

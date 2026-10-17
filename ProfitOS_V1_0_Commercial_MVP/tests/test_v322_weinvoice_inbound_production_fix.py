from pathlib import Path

W = Path('profitos/weinvoice.py').read_text(encoding='utf-8')
I = Path('profitos/routes/invoicing.py').read_text(encoding='utf-8')


def test_no_undefined_invoicing_token_helper_remains():
    assert 'fetch_invoicing_access_token()' not in W
    assert "fetch_access_token(credential_set='invoicing')" in W


def test_rejected_inbound_is_ignored_before_content_download():
    # v400 : le cœur de la synchro est extrait dans sync_weinvoice_inbound()
    # (appelé par la route et par le job planifié einvoice_inbound_sync).
    core = I[I.index('def sync_weinvoice_inbound('):I.index('def register(app):')]
    rejected = core.index("status in {'REJECTED','REJETEE','REJETÉE'}")
    content = core.index('content=get_inbound_invoice_content')
    assert rejected < content
    assert "regulatory == '213'" in core
    assert 'ignored+=1' in core
    block = I[I.index('def purchase_weinvoice_sync():'):I.index("@app.post('/facturation/achats/<int:purchase_id>/weinvoice/action')")]
    assert 'sync_weinvoice_inbound(c,eid,' in block
    assert 'rejetée(s) ignorée(s)' in block


def test_sync_log_exposes_ignored_count():
    assert 'created={created} skipped={skipped} ignored={ignored} failed={len(failed)}' in I

from pathlib import Path

INV = Path('profitos/routes/invoicing.py').read_text(encoding='utf-8')
RT = Path('profitos/runtime.py').read_text(encoding='utf-8')
TPL = Path('templates/purchase_sepa_batch.html').read_text(encoding='utf-8')


def _sepa_block():
    start = INV.index('def purchase_sepa_batch')
    end = INV.index("@app.route('/facturation/commandes')", start)
    return INV[start:end]


def test_sepa_export_does_not_book_or_mark_paid():
    block = _sepa_block()
    assert 'sepa_export_batches' in block
    assert 'sepa_export_items' in block
    assert 'generate_purchase_partial_payment_entry' not in block
    assert "UPDATE purchase_invoices SET status='paid'" not in block
    assert "purchase_invoice_payments" not in block
    assert "deliver_webhook(c, 'purchase.paid'" not in block


def test_sepa_export_registry_is_entity_scoped_and_auditable():
    assert 'CREATE TABLE IF NOT EXISTS sepa_export_batches' in RT
    assert 'CREATE TABLE IF NOT EXISTS sepa_export_items' in RT
    assert 'file_sha256 TEXT NOT NULL' in RT
    assert 'UNIQUE(entity_id,message_id)' in RT
    assert 'UNIQUE(entity_id,file_sha256)' in RT
    assert 'idx_sepa_export_items_invoice' in RT


def test_ui_explicitly_separates_export_from_payment_confirmation():
    assert 'Générer le fichier SEPA et marquer payées' not in TPL
    assert 'Générer le fichier SEPA' in TPL
    assert "L'export ne marque pas les factures comme payées" in TPL


def test_sepa_uses_remaining_supplier_balance_not_invoice_total():
    block = _sepa_block()
    assert "balance = _purchase_balance(c, r, entity_id)" in block
    assert "'amount': balance" in block
    assert "'amount': r['total']" not in block

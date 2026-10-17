from pathlib import Path

T = Path("templates/purchase_list.html").read_text(encoding="utf-8")
I = Path("profitos/routes/invoicing.py").read_text(encoding="utf-8")

def test_purchase_weinvoice_sync_button_is_csrf_protected():
    assert "Synchroniser les factures électroniques" in T
    assert "url_for('purchase_weinvoice_sync')" in T
    assert 'name="csrf_token"' in T
    assert "@app.route('/facturation/achats/weinvoice/synchroniser', methods=['POST'])" in I
    assert "def purchase_weinvoice_sync():" in I

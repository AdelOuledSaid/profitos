from pathlib import Path

R = Path('profitos/routes/invoicing.py').read_text(encoding='utf-8')
T = Path('templates/invoicing_list.html').read_text(encoding='utf-8')


def test_invoice_list_loads_entity_scoped_ereporting_history():
    assert 'FROM ereporting_transmissions WHERE entity_id IS ? ORDER BY id DESC LIMIT 25' in R
    assert 'ereporting_rows=ereporting_rows' in R


def test_dashboard_exposes_sync_proof_and_diagnostic_with_csrf():
    assert 'Suivi e-reporting' in T
    assert "url_for('invoicing_electronic_diagnostic')" in T
    assert "url_for('invoicing_ereporting_sync',transmission_id=t.id)" in T
    assert "url_for('invoicing_ereporting_proof',transmission_id=t.id)" in T
    assert T.count('name="csrf_token"') >= 2


def test_dashboard_does_not_claim_dgfip_filing():
    assert 'transmis à la DGFiP' not in T
    assert 'déposé à la DGFiP' not in T

from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def test_page_pro_dashboard_keeps_sandbox_and_human_controls():
    r=(ROOT/'profitos/routes/swan_baas.py').read_text(encoding='utf-8')
    t=(ROOT/'templates/swan_settings.html').read_text(encoding='utf-8')
    assert 'company = current_entity(c)' in r
    assert 'company=company' in r
    assert 'VUE D’ENSEMBLE' in t
    assert 'SANDBOX · TEST UNIQUEMENT' in t
    assert 'Banques & rapprochement' in t
    assert 'Comptes Swan visibles' in t
    assert 'Compte Pro' in t

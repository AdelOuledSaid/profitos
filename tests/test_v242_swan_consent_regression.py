from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
R = (ROOT / 'profitos/routes/swan_baas.py').read_text(encoding='utf-8')
T = (ROOT / 'templates/swan_settings.html').read_text(encoding='utf-8')


def test_v242_regression_uses_explicit_consent_link_not_cross_origin_post_redirect():
    assert "session['swan_transfer_consent_url'] = result['consent_url']" in R
    assert "return redirect(result['consent_url'])" not in R
    assert 'target="_blank"' in T
    assert 'Ouvrir la validation Swan' in T

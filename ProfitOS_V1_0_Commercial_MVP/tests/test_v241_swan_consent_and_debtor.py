from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
R = (ROOT/'profitos/routes/swan_baas.py').read_text(encoding='utf-8')
T = (ROOT/'templates/swan_settings.html').read_text(encoding='utf-8')

def test_explicit_consent_link_is_stored_and_rendered():
    assert "session['swan_transfer_consent_url'] = result['consent_url']" in R
    assert 'swan_transfer_consent_url=session.get' in R
    assert 'Ouvrir la validation Swan' in T
    assert 'href="{{ swan_transfer_consent_url }}"' in T

def test_debtor_account_is_explicitly_selected():
    assert '<select name="account_id" required>' in T
    assert 'Choisir explicitement le compte débiteur' in T
    assert "debtor.available_balance.get('value','—')" in T
    assert '<input type="hidden" name="account_id" value="{{ a.id or a.account_id }}">' not in T

def test_route_still_validates_account_ownership():
    assert "if account_id not in allowed:" in R
    assert "abort(403)" in R

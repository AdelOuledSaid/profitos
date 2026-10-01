from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
R = (ROOT / 'profitos/routes/swan_baas.py').read_text(encoding='utf-8')
T = (ROOT / 'templates/swan_settings.html').read_text(encoding='utf-8')
S = (ROOT / 'profitos/swan_baas.py').read_text(encoding='utf-8')


def test_transfer_requires_csrf_explicit_confirmation_and_swan_consent():
    assert 'confirm_sandbox_transfer' in R
    assert 'confirm_sandbox_transfer' in T
    assert 'name="csrf_token"' in T
    assert 'consentUrl' in S

    # v241+: do not rely on a cross-origin 302 after the POST.  Keep Swan's
    # consent URL server-side and expose it as an explicit user-opened link.
    assert "session['swan_transfer_consent_url'] = result['consent_url']" in R
    assert 'Ouvrir la validation Swan' in T
    assert 'href="{{ swan_transfer_consent_url }}"' in T


def test_transfer_remains_sandbox_only_and_uses_explicit_debtor_account():
    assert "if current_environment() != 'sandbox':" in R
    assert "account_id = (request.form.get('account_id') or '').strip()" in R
    assert '<select name="account_id" required>' in T
    assert 'Choisir explicitement le compte débiteur' in T

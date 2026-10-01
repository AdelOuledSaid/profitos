from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
S=(ROOT/'profitos'/'swan_baas.py').read_text(encoding='utf-8')
R=(ROOT/'profitos'/'routes'/'swan_baas.py').read_text(encoding='utf-8')
T=(ROOT/'templates'/'swan_settings.html').read_text(encoding='utf-8')

def test_sensitive_transfer_is_sandbox_only_and_uses_impersonation():
    assert "current_environment() != 'sandbox'" in S
    assert 'initiateCreditTransfers' in S
    assert "user_id=user_id" in S
    assert "'mode': 'Regular'" in S
    assert 'idempotencyKey' in S

def test_oauth_uses_state_and_discards_user_token():
    assert 'swan_oauth_state' in R
    assert 'compare_digest' in R
    assert "session['swan_user_id']" in R
    assert "session['swan_user_token']" not in R
    assert 'exchange_user_authorization_code' in S

def test_transfer_requires_csrf_explicit_confirmation_and_swan_consent():
    assert 'confirm_sandbox_transfer' in R
    assert 'confirm_sandbox_transfer' in T
    assert 'name="csrf_token"' in T
    assert 'consentUrl' in S
    assert "redirect(result['consent_url'])" in R

def test_transfer_account_is_checked_against_project_accounts():
    assert 'allowed = {' in R
    assert 'if account_id not in allowed' in R
    assert 'abort(403)' in R

from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

def src(rel): return (ROOT / rel).read_text(encoding='utf-8')

def test_swan_oauth_token_endpoint_is_official_token_endpoint():
    s=src('profitos/swan_baas.py')
    assert "TOKEN_URL = 'https://oauth.swan.io/oauth2/token'" in s

def test_swan_backend_graphql_and_optional_impersonation_header():
    s=src('profitos/swan_baas.py')
    assert 'https://api.swan.io/sandbox-partner/graphql' in s
    assert "'x-swan-user-id': str(user_id)" in s

def test_swan_webhook_is_secret_guarded_and_idempotent():
    r=src('profitos/routes/swan_baas.py')
    assert "request.headers.get('x-swan-secret')" in r or "req.headers.get('x-swan-secret')" in r
    assert "secrets.compare_digest(expected, supplied)" in r
    assert "SELECT id FROM swan_webhook_events WHERE event_id=?" in r
    assert "duplicate': True" in r

def test_swan_webhook_schema_and_csrf_exemption_exist():
    r=src('profitos/runtime.py')
    assert 'CREATE TABLE IF NOT EXISTS swan_webhook_events' in r
    assert "'/webhooks/swan'" in r

def test_unverified_account_card_mutations_fail_closed_by_default():
    s=src('profitos/swan_baas.py'); r=src('profitos/routes/swan_baas.py')
    assert 'SWAN_ENABLE_ACCOUNT_CARD_REQUESTS' in s
    assert 'write_operations_enabled()' in r
    assert 'désactivées tant que le flux' in r

def test_swan_ui_explains_sandbox_gate():
    t=src('templates/swan_settings.html')
    assert 'write_operations_enabled' in t
    assert 'Swan Sandbox' in t

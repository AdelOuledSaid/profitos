from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = (ROOT/"profitos"/"swan_baas.py").read_text(encoding="utf-8")
ROUTES = (ROOT/"profitos"/"routes"/"swan_baas.py").read_text(encoding="utf-8")
BASE = (ROOT/"templates"/"base.html").read_text(encoding="utf-8")
TPL = (ROOT/"templates"/"swan_settings.html").read_text(encoding="utf-8")

def test_swan_render_env_names_are_supported():
    assert "SWAN_ENVIRONMENT" in SERVICE
    assert "SWAN_ENV" in SERVICE
    assert "SWAN_GRAPHQL_URL" in SERVICE

def test_compte_pro_is_exposed_in_navigation():
    assert "url_for('swan_settings')" in BASE
    assert ">Compte Pro<" in BASE

def test_swan_connection_test_is_read_only():
    assert "/settings/swan/test-connection" in ROUTES
    assert "graphql_query(token, 'query ProfitOSConnectionTest { __typename }')" in ROUTES
    block = ROUTES.split("def swan_test_connection():",1)[1].split("@app.route('/settings/swan/compte/nouveau'",1)[0]
    assert "request_new_account(" not in block
    assert "request_card(" not in block

def test_swan_connection_test_has_csrf_form():
    assert "url_for('swan_test_connection')" in TPL
    assert 'name="csrf_token"' in TPL
    assert "Tester la connexion Swan Sandbox" in TPL

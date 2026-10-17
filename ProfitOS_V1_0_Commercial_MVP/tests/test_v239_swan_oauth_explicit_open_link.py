from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
R=(ROOT/'profitos'/'routes'/'swan_baas.py').read_text(encoding='utf-8')
T=(ROOT/'templates'/'swan_settings.html').read_text(encoding='utf-8')

def test_oauth_url_is_prepared_server_side_and_exposed_as_get_link():
    assert "session['swan_oauth_open_url'] = build_user_authorization_url(state)" in R
    assert "swan_oauth_open_url=session.get('swan_oauth_open_url')" in R
    assert 'href="{{ swan_oauth_open_url }}"' in T
    assert 'Ouvrir Swan Sandbox' in T

def test_oauth_state_and_csrf_protections_are_preserved():
    assert "session['swan_oauth_state'] = state" in R
    assert 'compare_digest' in R
    assert 'name="csrf_token"' in T

def test_debug_oauth_link_is_removed_on_callback_or_error():
    assert "session.pop('swan_oauth_open_url', None)" in R

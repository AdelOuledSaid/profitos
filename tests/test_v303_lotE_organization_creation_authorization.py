from pathlib import Path
T=Path("profitos/routes/account.py").read_text(encoding="utf-8")

def _decorators(route, func):
    return T.split(route,1)[1].split(func,1)[0]

def test_new_organization_requires_settings_permission():
    b=_decorators("@app.route('/organizations/new'", "def org_new():")
    assert "@login_required" in b
    assert "@require_area('settings')" in b

def test_org_switch_remains_available_to_members_of_target_org():
    b=_decorators("@app.route('/organizations/switch/<int:org_id>'", "def org_switch(org_id):")
    assert "@login_required" in b
    assert "@require_area('settings')" not in b
    body=T.split("def org_switch(org_id):",1)[1].split("@app.route('/signup'",1)[0]
    assert "organization_id=?" in body
    assert "session['user_id']" in body

def test_previous_lotE_guards_are_preserved():
    for route,func in [
        ("@app.route('/settings/gdpr-export')","def gdpr_export():"),
        ("@app.route('/settings/send-accountant-export'","def send_accountant_export_now():"),
        ("@app.route('/settings/test-notification'","def test_notification():"),
    ]:
        assert "@require_area('settings')" in _decorators(route,func)

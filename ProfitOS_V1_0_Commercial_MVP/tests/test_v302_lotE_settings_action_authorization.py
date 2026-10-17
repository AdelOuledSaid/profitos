from pathlib import Path
T=Path("profitos/routes/account.py").read_text(encoding="utf-8")

def _decorators(route, func):
    b=T.split(route,1)[1].split(func,1)[0]
    return b

def test_accountant_export_requires_settings_permission():
    b=_decorators("@app.route('/settings/send-accountant-export'", "def send_accountant_export_now():")
    assert "@login_required" in b
    assert "@require_area('settings')" in b

def test_notification_test_requires_settings_permission():
    b=_decorators("@app.route('/settings/test-notification'", "def test_notification():")
    assert "@login_required" in b
    assert "@require_area('settings')" in b

def test_v301_gdpr_export_protection_is_preserved():
    b=_decorators("@app.route('/settings/gdpr-export')", "def gdpr_export():")
    assert "@require_area('settings')" in b

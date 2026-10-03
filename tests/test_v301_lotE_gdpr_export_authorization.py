from pathlib import Path
T=Path("profitos/routes/account.py").read_text(encoding="utf-8")

def test_org_wide_gdpr_export_requires_settings_permission():
    b=T.split("@app.route('/settings/gdpr-export')",1)[1].split("def gdpr_export():",1)[0]
    assert "@login_required" in b
    assert "@require_area('settings')" in b

def test_settings_permission_is_not_added_to_account_deletion():
    b=T.split("@app.route('/settings/delete-account'",1)[1].split("def delete_account():",1)[0]
    assert "@login_required" in b
    assert "@require_area('settings')" not in b

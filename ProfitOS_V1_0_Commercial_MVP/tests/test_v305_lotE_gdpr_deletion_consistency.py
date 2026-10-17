from pathlib import Path
T=Path("profitos/routes/account.py").read_text(encoding="utf-8")
# v4xx : la purge définitive (après le délai de grâce de 14 jours) vit dans profitos/retention.py.
R=Path("profitos/retention.py").read_text(encoding="utf-8")
H=R.split("def hard_delete_organization(",1)[1].split("\ndef ",1)[0]

def test_gdpr_delete_covers_current_shared_auth_tables():
    for table in (
        "accounting_fec_tokens","supplier_inbox_tokens","supplier_inbox_entity_tokens",
        "outgoing_invoice_tokens","outgoing_quote_tokens","api_audit_log","api_idempotency",
        "cabinet_time_entries","cabinet_documents","team_invitations","swan_account_bindings"
    ):
        assert f"('{table}', 'organization_id')" in R

def test_gdpr_auth_cleanup_is_one_transaction_without_silent_pass():
    assert "for table, column in present:" in H
    assert "ac.commit()" in H
    assert "ac.rollback()" in H
    assert "except Exception:\n        pass" not in H

def test_gdpr_deletion_is_scheduled_with_grace_period_and_reauth():
    B=T.split("def delete_account():",1)[1].split("@app.route('/changelog/seen'",1)[0]
    assert "schedule_organization_deletion(" in B
    assert "acsec.verify_reauth(user)" in B
    assert "GRACE_PERIOD_DAYS = 14" in R
    assert "def purge_deleted_organizations(" in R

def test_previous_lotE_permission_guards_preserved():
    for route,func in [
        ("@app.route('/settings/gdpr-export')","def gdpr_export():"),
        ("@app.route('/settings/send-accountant-export'","def send_accountant_export_now():"),
        ("@app.route('/settings/test-notification'","def test_notification():"),
        ("@app.route('/organizations/new'","def org_new():"),
    ]:
        d=T.split(route,1)[1].split(func,1)[0]
        assert "@require_area('settings')" in d

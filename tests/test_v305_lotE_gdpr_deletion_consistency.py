from pathlib import Path
T=Path("profitos/routes/account.py").read_text(encoding="utf-8")
B=T.split("def delete_account():",1)[1].split("@app.route('/changelog/seen'",1)[0]

def test_gdpr_delete_covers_current_shared_auth_tables():
    for table in (
        "accounting_fec_tokens","supplier_inbox_tokens","supplier_inbox_entity_tokens",
        "outgoing_invoice_tokens","outgoing_quote_tokens","api_audit_log","api_idempotency",
        "cabinet_time_entries","cabinet_documents"
    ):
        assert f"('{table}','organization_id')" in B

def test_gdpr_auth_cleanup_is_one_transaction_without_silent_pass():
    section=B.split("auth_deletes=(",1)[1].split("remaining_members=0",1)[0]
    assert "for table,column in auth_deletes:" in section
    assert "ac.commit()" in section
    assert "ac.rollback()" in section
    assert "except Exception:\n                    pass" not in section

def test_gdpr_does_not_claim_total_deletion_on_auth_cleanup_failure():
    section=B.split("auth_deletes=(",1)[1].split("remaining_members=0",1)[0]
    assert "Aucune confirmation de suppression totale" in section
    assert "session.clear()" in section

def test_previous_lotE_permission_guards_preserved():
    for route,func in [
        ("@app.route('/settings/gdpr-export')","def gdpr_export():"),
        ("@app.route('/settings/send-accountant-export'","def send_accountant_export_now():"),
        ("@app.route('/settings/test-notification'","def test_notification():"),
        ("@app.route('/organizations/new'","def org_new():"),
    ]:
        d=T.split(route,1)[1].split(func,1)[0]
        assert "@require_area('settings')" in d

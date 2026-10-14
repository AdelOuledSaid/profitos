"""V4 — baisse de formule, parrainage et quota d'extractions IA (items 7, 8, 9)."""
from datetime import datetime, timedelta, timezone

import pytest

from v4_billing_support import billing_env, make_org, make_user, login, org_row  # noqa: F401

from profitos import runtime
from profitos.plan_limits import AI_EXTRACTIONS_PER_MONTH, plan_limit
from profitos.plan_usage import AIQuotaExceeded, consume_ai_extraction_quota, usage_count


# --- Item 7 : dépassements après une baisse de formule -----------------------

def test_downgrade_overage_listed_on_billing_and_team(billing_env):
    oid = make_org(plan="STARTER", status="ACTIVE_PAID", trial_days=None)
    owner = make_user(oid, name="Patronne")
    make_user(oid, "MEMBER", name="Alice")
    make_user(oid, "MEMBER", name="Bruno")
    over = runtime.plan_overage(org_row(oid))
    assert over["team_limit"] == 2 and over["members"] == ["Bruno"]
    client = billing_env.app.test_client()
    login(client, owner, oid)
    html = client.get("/billing").get_data(as_text=True)
    assert "Votre formule actuelle (Starter) est dépassée" in html and "Bruno" in html
    team = client.get("/team").get_data(as_text=True)
    assert "Au-delà de cette limite : Bruno" in team
    # Aucun nouvel utilisateur ne peut être invité.
    client.post("/team/invite", data={"csrf_token": "csrf-test", "email": "new@example.fr", "role": "MEMBER"})
    c = runtime.auth_cx()
    n = c.execute("SELECT COUNT(*) n FROM memberships WHERE organization_id=?", (oid,)).fetchone()["n"]
    c.close()
    assert n == 3


def test_multi_downgrade_lists_extra_organizations(billing_env):
    parent = make_org(name="Mère", plan="PRO", status="ACTIVE_PAID", trial_days=None)
    make_org(name="Fille A", plan="PRO", status="ACTIVE_PAID", trial_days=None, parent=parent)
    over = runtime.plan_overage(org_row(parent))
    assert over["organizations"] == ["Fille A"]


# --- Item 8 : parrainage ------------------------------------------------------

def _referral(referrer, referred):
    c = runtime.auth_cx()
    c.execute("INSERT INTO referrals(referrer_org_id,referred_org_id,status,created_at) VALUES(?,?,?,?)",
              (referrer, referred, "PENDING", runtime.now()))
    c.commit(); c.close()


def _reward(app, referred):
    with app.app_context():
        c = runtime.auth_cx()
        runtime.reward_referrer_if_any(c, referred)
        c.commit(); c.close()


def test_paying_referrer_gets_stripe_balance_credit(billing_env):
    referrer = make_org(plan="PRO", status="ACTIVE_PAID", trial_days=None, customer="cus_ref", subscription="sub_ref")
    referred = make_org(name="Filleul")
    _referral(referrer, referred)
    before = org_row(referrer)["trial_ends_at"]
    _reward(billing_env.app, referred)
    (call,) = billing_env.stripe.called("Customer.create_balance_transaction")
    assert call[1] == "cus_ref" and call[2]["amount"] == -2900 and call[2]["currency"] == "eur"
    assert org_row(referrer)["trial_ends_at"] == before


def test_trialing_referrer_gets_30_more_days(billing_env):
    referrer = make_org(trial_days=5)
    referred = make_org(name="Filleul")
    _referral(referrer, referred)
    _reward(billing_env.app, referred)
    end = runtime.parse_dt(org_row(referrer)["trial_ends_at"])
    assert end > datetime.now(timezone.utc) + timedelta(days=34)


def test_expired_trial_referrer_is_not_revived_but_credit_is_kept(billing_env):
    referrer = make_org(trial_days=-10, customer="cus_ref")
    referred = make_org(name="Filleul")
    _referral(referrer, referred)
    _reward(billing_env.app, referred)
    row = org_row(referrer)
    assert runtime.org_has_access(row) is False
    assert row["referral_credit_months"] == 1
    assert not billing_env.stripe.called("Customer.create_balance_transaction")
    # Imputé au premier paiement.
    c = runtime.auth_cx()
    c.execute("UPDATE organizations SET plan='PRO',status='ACTIVE_PAID' WHERE id=?", (referrer,))
    c.commit()
    with billing_env.app.app_context():
        runtime.apply_pending_referral_credit(c, referrer)
    c.commit(); c.close()
    assert billing_env.stripe.called("Customer.create_balance_transaction")
    assert org_row(referrer)["referral_credit_months"] == 0


def test_referral_page_describes_real_behaviour(billing_env):
    oid = make_org(trial_days=5)
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    html = client.get("/referral").get_data(as_text=True)
    assert "solde Stripe" in html and "prolongé de 30 jours" in html
    assert "ne réactive pas un essai expiré" in html
    assert "tu reçois" not in html


# --- Item 9 : quota d'extractions IA -----------------------------------------

def test_ai_quota_numbers_are_centralised():
    assert AI_EXTRACTIONS_PER_MONTH == {"TRIAL": 20, "STARTER": 30, "PRO": 200, "BUSINESS": 1000, "MULTI": 3000}
    for plan, quota in AI_EXTRACTIONS_PER_MONTH.items():
        assert plan_limit(plan, "ai_extractions_per_month") == quota


def test_ai_quota_consumed_then_refused_with_french_message(billing_env):
    oid = make_org(plan="STARTER", status="ACTIVE_PAID", trial_days=None)
    for _ in range(30):
        consume_ai_extraction_quota(oid)
    assert usage_count("ai_extractions_per_month", organization_id=oid) == 30
    with pytest.raises(AIQuotaExceeded) as exc:
        consume_ai_extraction_quota(oid)
    assert isinstance(exc.value, ValueError)
    assert "quota mensuel d'extractions automatiques par IA" in str(exc.value)
    assert "manuellement" in str(exc.value)


def test_ai_quota_of_child_counts_on_paying_parent(billing_env):
    parent = make_org(plan="MULTI", status="ACTIVE_PAID", trial_days=None)
    child = make_org(name="Fille", plan="MULTI", status="ACTIVE_PAID", trial_days=None, parent=parent)
    consume_ai_extraction_quota(child)
    assert usage_count("ai_extractions_per_month", organization_id=parent) == 1


def test_every_anthropic_call_site_consumes_quota_before_calling():
    root = runtime.BASE / "profitos" / "routes"
    for fname, funcs in {
        "invoicing.py": ["def _purchase_ai_extract("],
        "expenses.py": ["def _receipt_ai_extract("],
        "imports.py": ["def _ai_extract_pdf_fields(", "def _ai_extract_bank_statement_rows("],
    }.items():
        src = (root / fname).read_text(encoding="utf-8")
        for f in funcs:
            body = src.split(f, 1)[1]
            body = body.split("\ndef ", 1)[0]
            assert "consume_ai_extraction_quota()" in body, (fname, f)
            assert body.index("consume_ai_extraction_quota()") < body.index("api.anthropic.com"), (fname, f)
    inbox = (root / "invoicing.py").read_text(encoding="utf-8")
    assert "g.ai_quota_org_id = org_id" in inbox


def test_receipt_extraction_falls_back_when_quota_exhausted(billing_env, monkeypatch):
    from profitos.routes import expenses
    monkeypatch.setattr(expenses, "ANTHROPIC_API_KEY", "test-key")
    called = []
    monkeypatch.setattr(expenses.requests, "post", lambda *a, **k: called.append(1))
    oid = make_org(plan="STARTER", status="ACTIVE_PAID", trial_days=None)
    for _ in range(30):
        consume_ai_extraction_quota(oid)
    with billing_env.app.test_request_context("/x"):
        from flask import g
        g.ai_quota_org_id = oid
        with pytest.raises(ValueError):
            expenses._receipt_ai_extract(b"%PDF", "application/pdf")
    assert called == []  # aucun appel Anthropic une fois le quota atteint


def test_invitation_sent_before_downgrade_cannot_exceed_new_limit(billing_env):
    oid = make_org(plan="STARTER", status="ACTIVE_PAID", trial_days=None)
    make_user(oid, name="Patronne")
    make_user(oid, "MEMBER", name="Alice")  # 2/2 utilisateurs Starter
    guest = make_user(make_org(name="Ailleurs"), "OWNER", email="invite@example.fr")
    token = "jeton-invitation-test"
    c = runtime.auth_cx()
    c.execute("INSERT INTO team_invitations(organization_id,email,role,token_hash,created_at,expires_at) "
              "VALUES(?,?,?,?,?,?)", (oid, "invite@example.fr", "MEMBER", runtime.token_digest(token),
                                      runtime.now(), (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()))
    c.commit(); c.close()
    assert runtime.team_seat_available(oid) is False
    client = billing_env.app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = guest; s["org_id"] = oid; s["csrf_token"] = "csrf-test"; s["auth_version"] = 0
    client.post(f"/invitation/{token}", data={"csrf_token": "csrf-test", "action": "accept"})
    c = runtime.auth_cx()
    n = c.execute("SELECT COUNT(*) n FROM memberships WHERE organization_id=?", (oid,)).fetchone()["n"]
    c.close()
    assert n == 2


def test_cloud_folder_sync_counts_ai_quota_on_the_synced_org(billing_env, monkeypatch):
    from flask import g
    from profitos.routes import cloud_storage
    oid = make_org(plan="PRO", status="ACTIVE_PAID", trial_days=None)
    monkeypatch.setattr(cloud_storage, "list_folder_files", lambda c, conn: [])
    tc = runtime.tenant_cx_direct(oid)
    try:
        with billing_env.app.app_context():
            assert cloud_storage.sync_cloud_folder(tc, {"id": 1}, "dropbox", None, oid) == ([], [], 0)
            assert g.ai_quota_org_id == oid  # tâche nocturne sans session : quota de cette organisation
    finally:
        tc.close()

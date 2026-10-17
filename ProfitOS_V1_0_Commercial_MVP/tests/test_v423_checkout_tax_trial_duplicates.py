"""V4 — Checkout Stripe : TVA, conservation des jours d'essai, anti double
abonnement (items 4 et 5)."""
from datetime import datetime, timezone

from v4_billing_support import billing_env, make_org, make_user, login, org_row  # noqa: F401

from profitos import runtime


def _checkout(client, plan="PRO"):
    return client.post("/billing/checkout", data={"csrf_token": "csrf-test", "plan": plan})


def test_checkout_enables_vat_and_keeps_remaining_trial_days(billing_env):
    oid = make_org(trial_days=10, customer="cus_1")
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    r = _checkout(client)
    assert r.status_code == 303 and "checkout.stripe.com" in r.headers["Location"]
    (_, kw), = billing_env.stripe.called("checkout.Session.create")
    assert kw["automatic_tax"] == {"enabled": True}
    assert kw["tax_id_collection"] == {"enabled": True}
    assert kw["billing_address_collection"] == "required"
    assert kw["customer_update"] == {"address": "auto", "name": "auto"}
    assert kw["locale"] == "fr" and kw["allow_promotion_codes"] is True
    trial_end = kw["subscription_data"]["trial_end"]
    expected = runtime.parse_dt(org_row(oid)["trial_ends_at"]).timestamp()
    assert abs(trial_end - expected) < 2
    assert kw["subscription_data"]["metadata"]["organization_id"] == str(oid)


def test_checkout_without_trial_has_no_trial_end(billing_env):
    oid = make_org(trial_days=-3, customer="cus_1")
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    _checkout(client)
    (_, kw), = billing_env.stripe.called("checkout.Session.create")
    assert "trial_end" not in kw["subscription_data"]


def test_automatic_tax_can_be_disabled_by_env(billing_env, monkeypatch):
    from profitos.routes import account
    monkeypatch.setattr(account, "STRIPE_AUTOMATIC_TAX", False)
    oid = make_org(trial_days=10, customer="cus_1")
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    _checkout(client)
    (_, kw), = billing_env.stripe.called("checkout.Session.create")
    assert kw["automatic_tax"] == {"enabled": False}


def test_existing_live_subscription_in_stripe_redirects_to_portal(billing_env):
    oid = make_org(trial_days=10, customer="cus_1")
    owner = make_user(oid)
    billing_env.stripe.customer_subscriptions = [{"id": "sub_x", "status": "trialing"}]
    client = billing_env.app.test_client()
    login(client, owner, oid)
    r = _checkout(client)
    assert r.status_code == 303 and "billing.stripe.com" in r.headers["Location"]
    assert not billing_env.stripe.called("checkout.Session.create")


def test_open_checkout_sessions_are_expired_before_new_one(billing_env):
    oid = make_org(trial_days=10, customer="cus_1")
    owner = make_user(oid)
    billing_env.stripe.open_sessions = [{"id": "cs_old_1"}, {"id": "cs_old_2"}]
    client = billing_env.app.test_client()
    login(client, owner, oid)
    _checkout(client)
    expired = [c[1] for c in billing_env.stripe.called("checkout.Session.expire")]
    assert expired == ["cs_old_1", "cs_old_2"]
    assert billing_env.stripe.called("checkout.Session.create")


def test_paying_org_checkout_goes_back_to_plan_change(billing_env):
    # Déjà abonné : jamais de second abonnement ; le changement se fait par
    # « Passer à cette formule » sur la page Abonnement.
    oid = make_org(plan="PRO", status="ACTIVE_PAID", trial_days=None, customer="cus_1", subscription="sub_1")
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    r = _checkout(client, "BUSINESS")
    assert r.status_code == 302 and r.headers["Location"].endswith("/billing")
    assert not billing_env.stripe.called("checkout.Session.create")


def test_stripe_list_failure_blocks_checkout(billing_env):
    oid = make_org(trial_days=10, customer="cus_1")
    owner = make_user(oid)

    def boom(**kw):
        raise RuntimeError("API down")
    billing_env.stripe.Subscription.list = staticmethod(boom)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    r = _checkout(client)
    assert r.status_code == 302 and r.headers["Location"].endswith("/billing")
    assert not billing_env.stripe.called("checkout.Session.create")


def test_child_org_cannot_start_its_own_checkout(billing_env):
    parent = make_org(plan="MULTI", status="ACTIVE_PAID", trial_days=None, customer="cus_1", subscription="sub_1")
    child = make_org(name="Filiale", plan="MULTI", status="ACTIVE_PAID", trial_days=None, parent=parent)
    owner = make_user(child)
    client = billing_env.app.test_client()
    login(client, owner, child)
    r = _checkout(client)
    assert r.status_code == 302
    assert not billing_env.stripe.called("checkout.Session.create")


def test_prices_are_displayed_excluding_vat(billing_env):
    oid = make_org(trial_days=10)
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    html = client.get("/billing").get_data(as_text=True)
    assert "€ <small>HT / mois</small>" in html
    assert "TVA 20 % en sus" in html
    assert "€ / mois" not in html and "€ <small>/ mois</small>" not in html
    public = client.get("/pricing").get_data(as_text=True)
    assert "€ <small>HT / mois</small>" in public and "TVA 20 % en sus" in public
    assert "34,80&nbsp;€ TTC" in public  # 29 € HT x 1,2 au format français

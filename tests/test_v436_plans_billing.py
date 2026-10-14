"""v436 — Nouveaux tarifs (9/29/59/199 € HT), annuel avec 2 mois offerts, changement de
formule en un clic, fonctionnalités verrouillées avec la formule nécessaire."""
from pathlib import Path

import pytest

from v4_billing_support import billing_env, make_org, make_user, login, org_row  # noqa: F401
from profitos import runtime

ROOT = Path(__file__).resolve().parents[1]


def _sub(price, interval="month", period_end=1893456000):
    return {"id": "sub_1", "status": "active", "customer": "cus_1", "current_period_end": period_end,
            "items": {"data": [{"id": "si_1", "price": {"id": price, "recurring": {"interval": interval}}}]}}


@pytest.fixture
def modify_spy(billing_env):
    calls = []

    def modify(sub_id, **kw):
        calls.append((sub_id, kw))
        return {"id": sub_id, "pending_update": None}
    billing_env.stripe.Subscription.modify = staticmethod(modify)
    return calls


def _change(client, plan, interval="month"):
    return client.post("/billing/changer-formule", data={"csrf_token": "csrf-test", "plan": plan, "interval": interval})


def _flashes(client):
    with client.session_transaction() as s:
        return " ".join(m for _, m in s.get("_flashes", []))


# ------------------------------------------------------------------ tarifs
def test_new_prices_and_annual_is_ten_months():
    plans = runtime.STRIPE_PLANS
    assert [plans[k]["price_eur"] for k in runtime.PLAN_ORDER] == [9, 29, 59, 199]
    assert [plans[k]["price_year_eur"] for k in runtime.PLAN_ORDER] == [90, 290, 590, 1990]


def test_annual_option_hidden_until_yearly_prices_configured(billing_env, monkeypatch):
    oid = make_org(trial_days=10, customer="cus_1")
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    html = client.get("/billing").get_data(as_text=True)
    assert "2 mois offerts" not in html
    monkeypatch.setitem(runtime.STRIPE_PLANS["PRO"], "price_id_year", "price_pro_year")
    html = client.get("/billing?periode=annuel").get_data(as_text=True)
    assert "2 mois offerts" in html and "290&nbsp;€ <small>HT / an</small>" in html
    assert 'name="interval" value="year"' in html


def test_checkout_uses_yearly_price_when_requested(billing_env, monkeypatch):
    monkeypatch.setitem(runtime.STRIPE_PLANS["PRO"], "price_id_year", "price_pro_year")
    oid = make_org(trial_days=10, customer="cus_1")
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    r = client.post("/billing/checkout", data={"csrf_token": "csrf-test", "plan": "PRO", "interval": "year"})
    assert r.status_code == 303
    (_, kw), = billing_env.stripe.called("checkout.Session.create")
    assert kw["line_items"][0]["price"] == "price_pro_year"
    assert kw["subscription_data"]["metadata"]["interval"] == "year"


def test_checkout_yearly_refused_when_not_configured(billing_env):
    oid = make_org(trial_days=10, customer="cus_1")
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    r = client.post("/billing/checkout", data={"csrf_token": "csrf-test", "plan": "PRO", "interval": "year"})
    assert r.status_code == 302 and not billing_env.stripe.called("checkout.Session.create")


# ------------------------------------------------------------------ changement de formule
def test_upgrade_is_immediate_and_prorated(billing_env, modify_spy):
    oid = make_org(plan="PRO", status="ACTIVE_PAID", trial_days=None, customer="cus_1", subscription="sub_1")
    billing_env.stripe.subscriptions["sub_1"] = _sub("price_pro_test")
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    r = _change(client, "BUSINESS")
    assert r.status_code == 302
    (sub_id, kw), = modify_spy
    assert sub_id == "sub_1" and kw["items"] == [{"id": "si_1", "price": "price_business_test"}]
    assert kw["proration_behavior"] == "always_invoice" and kw["payment_behavior"] == "pending_if_incomplete"
    assert org_row(oid)["plan"] == "BUSINESS"
    assert "Business" in _flashes(client)


def test_failed_upgrade_payment_keeps_plan(billing_env):
    oid = make_org(plan="PRO", status="ACTIVE_PAID", trial_days=None, customer="cus_1", subscription="sub_1")
    billing_env.stripe.subscriptions["sub_1"] = _sub("price_pro_test")
    billing_env.stripe.Subscription.modify = staticmethod(lambda sub_id, **kw: {"id": sub_id, "pending_update": {"expires_at": 1}})
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    _change(client, "BUSINESS")
    assert org_row(oid)["plan"] == "PRO"
    assert "n'a pas abouti" in _flashes(client)


def test_downgrade_monthly_is_immediate_with_credit(billing_env, modify_spy):
    oid = make_org(plan="BUSINESS", status="ACTIVE_PAID", trial_days=None, customer="cus_1", subscription="sub_1")
    billing_env.stripe.subscriptions["sub_1"] = _sub("price_business_test")
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    _change(client, "PRO")
    (_, kw), = modify_spy
    assert kw["proration_behavior"] == "create_prorations" and "payment_behavior" not in kw
    assert org_row(oid)["plan"] == "PRO"


def test_downgrade_refused_when_team_exceeds_new_limit(billing_env, modify_spy):
    oid = make_org(plan="PRO", status="ACTIVE_PAID", trial_days=None, customer="cus_1", subscription="sub_1")
    billing_env.stripe.subscriptions["sub_1"] = _sub("price_pro_test")
    owner = make_user(oid)
    make_user(oid, "MEMBER"); make_user(oid, "MEMBER")  # 3 utilisateurs, Starter en inclut 2
    client = billing_env.app.test_client()
    login(client, owner, oid)
    _change(client, "STARTER")
    assert not modify_spy and org_row(oid)["plan"] == "PRO"
    assert "retirez 1 accès" in _flashes(client)


def test_annual_subscription_cannot_downgrade_before_term(billing_env, modify_spy, monkeypatch):
    monkeypatch.setitem(runtime.STRIPE_PLANS["PRO"], "price_id_year", "price_pro_year")
    oid = make_org(plan="BUSINESS", status="ACTIVE_PAID", trial_days=None, customer="cus_1", subscription="sub_1")
    billing_env.stripe.subscriptions["sub_1"] = _sub("price_business_year", interval="year")
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    _change(client, "PRO", "year")
    _change(client, "BUSINESS", "month")
    assert not modify_spy and org_row(oid)["plan"] == "BUSINESS"
    assert "abonnement annuel court jusqu'au" in _flashes(client)


def test_monthly_to_annual_same_plan_is_an_upgrade(billing_env, modify_spy, monkeypatch):
    monkeypatch.setitem(runtime.STRIPE_PLANS["PRO"], "price_id_year", "price_pro_year")
    oid = make_org(plan="PRO", status="ACTIVE_PAID", trial_days=None, customer="cus_1", subscription="sub_1")
    billing_env.stripe.subscriptions["sub_1"] = _sub("price_pro_test")
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    _change(client, "PRO", "year")
    (_, kw), = modify_spy
    assert kw["items"][0]["price"] == "price_pro_year" and kw["proration_behavior"] == "always_invoice"


def test_only_owner_changes_plan(billing_env, modify_spy):
    oid = make_org(plan="PRO", status="ACTIVE_PAID", trial_days=None, customer="cus_1", subscription="sub_1")
    make_user(oid)
    member = make_user(oid, "ADMIN")
    client = billing_env.app.test_client()
    login(client, member, oid, "ADMIN")
    r = _change(client, "BUSINESS")
    assert r.status_code in (302, 403) and not modify_spy


def test_billing_page_offers_one_click_change_for_subscribers(billing_env):
    oid = make_org(plan="PRO", status="ACTIVE_PAID", trial_days=None, customer="cus_1", subscription="sub_1")
    billing_env.stripe.subscriptions["sub_1"] = _sub("price_pro_test")
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    html = client.get("/billing").get_data(as_text=True)
    assert "VOTRE FORMULE" in html and "Formule actuelle" in html
    assert "Passer à Business" in html and "Descendre à Starter" in html
    assert "/billing/changer-formule" in html and "data-confirm=" in html


# ------------------------------------------------------------------ fonctions verrouillées
def test_locked_feature_names_the_plan_and_price(billing_env):
    oid = make_org(plan="STARTER", status="ACTIVE_PAID", trial_days=None)
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    r = client.get("/comptabilite/emprunts")
    assert r.status_code == 302 and "/billing?formule=PRO#formules" in r.headers["Location"]
    assert "formule Pro (29 € HT / mois)" in _flashes(client)
    html = client.get("/billing?formule=PRO").get_data(as_text=True)
    assert "NÉCESSAIRE POUR CETTE FONCTION" in html


def test_menu_shows_locked_modules_with_required_plan(billing_env):
    oid = make_org(plan="STARTER", status="ACTIVE_PAID", trial_days=None)
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    html = client.get("/").get_data(as_text=True)
    nav = html[html.find('<nav aria-label="Navigation principale">'):html.find('</nav>')]
    assert 'class="nav-locked"' in nav
    assert "Comptabilité <small>Pro</small>" in nav
    assert "Banques <small>Pro</small>" in nav and "Scénarios <small>Business</small>" in nav


def test_cgv_describe_monthly_and_annual_terms():
    t = (ROOT / "templates/legal.html").read_text(encoding="utf-8")
    assert "Mensuelle, sans engagement" in t and "engagement de 12 mois" in t
    assert "<h3>Changement de formule</h3>" in t

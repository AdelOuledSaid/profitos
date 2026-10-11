"""V4 — garde global d'abonnement et de formule (items 2, 3 et 10 de l'audit)."""
from datetime import datetime, timedelta, timezone

from v4_billing_support import billing_env, make_org, make_user, login, iso  # noqa: F401

from profitos import runtime
from profitos.plan_limits import feature_enabled, PLAN_LIMITS


def _loc(r):
    return r.headers.get("Location", "")


# --- Item 3 : plus de boucle de redirection pour les rôles non propriétaires ---

def test_expired_trial_member_reaches_inactive_page_without_loop(billing_env):
    oid = make_org(trial_days=-1)
    owner = make_user(oid, "OWNER", email="patron@example.fr", name="Jeanne Patron")
    member = make_user(oid, "COMPTABLE")
    client = billing_env.app.test_client()
    login(client, member, oid, "COMPTABLE")
    r = client.get("/")
    assert r.status_code == 302 and "/abonnement-inactif" in _loc(r)
    page = client.get("/abonnement-inactif")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Abonnement inactif" in html
    assert "Contactez le propriétaire du compte" in html
    assert "patron@example.fr" in html and "Jeanne Patron" in html
    # Le comptable ne voit pas le lien de facturation (réservé au propriétaire).
    assert "Voir les formules et la facturation" not in html
    # requires_active_plan ne redirige plus vers /billing.
    assert "redirect(url_for('billing'))" not in runtime.requires_active_plan.__code__.co_consts


def test_expired_trial_owner_gets_billing_cta_and_billing_page(billing_env):
    oid = make_org(trial_days=-1)
    owner = make_user(oid, "OWNER")
    client = billing_env.app.test_client()
    login(client, owner, oid)
    page = client.get("/abonnement-inactif")
    assert page.status_code == 200
    assert "Voir les formules et la facturation" in page.get_data(as_text=True)
    assert client.get("/billing").status_code == 200


def test_inactive_page_redirects_home_when_access_is_valid(billing_env):
    oid = make_org(trial_days=5)
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    r = client.get("/abonnement-inactif")
    assert r.status_code == 302 and _loc(r).endswith("/")


# --- Item 2 : contrôle global de formule ---

STARTER_BLOCKED = [
    "/comptabilite/emprunts", "/comptabilite/paie", "/comptabilite/revision",
    "/comptabilite/credit-bail", "/comptabilite/analytique", "/cabinet/portefeuille",
    "/portfolio", "/entites", "/settings/webhooks", "/integrations/ecommerce",
    "/settings/swan", "/banking",
]


def test_starter_cannot_reach_pro_and_multi_modules(billing_env):
    oid = make_org(plan="STARTER", status="ACTIVE_PAID", trial_days=None)
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    for path in STARTER_BLOCKED:
        r = client.get(path)
        assert r.status_code == 302, path
        assert "/billing" in _loc(r), path
    r = client.post("/settings/swan/transfer/sepa", data={"csrf_token": "csrf-test"})
    assert r.status_code == 302 and "/billing" in _loc(r)


def test_non_owner_feature_denial_does_not_target_owner_only_billing(billing_env):
    oid = make_org(plan="STARTER", status="ACTIVE_PAID", trial_days=None)
    make_user(oid)
    member = make_user(oid, "MEMBER")
    client = billing_env.app.test_client()
    login(client, member, oid, "MEMBER")
    r = client.get("/comptabilite/emprunts")
    assert r.status_code == 302
    assert "/abonnement-inactif" in _loc(r) and "raison=formule" in _loc(r)
    assert client.get(_loc(r)).status_code == 200


def test_pro_reaches_accounting_but_not_multi_entity(billing_env):
    oid = make_org(plan="PRO", status="ACTIVE_PAID", trial_days=None)
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    r = client.get("/comptabilite/emprunts")
    assert r.status_code == 200
    r = client.get("/entites")
    assert r.status_code == 302 and "/billing" in _loc(r)


def test_valid_trial_has_exactly_pro_features(billing_env):
    for feature, value in PLAN_LIMITS["PRO"].items():
        if feature == "ai_extractions_per_month":
            continue
        assert PLAN_LIMITS["TRIAL"][feature] == value, feature
    oid = make_org(trial_days=7)
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    assert client.get("/comptabilite/emprunts").status_code == 200
    assert client.get("/entites").status_code == 302


def test_canceled_paid_plan_is_blocked_everywhere_but_exports(billing_env):
    oid = make_org(plan="PRO", status="CANCELED", trial_days=None)
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    for path in ("/", "/comptabilite/emprunts", "/facturation", "/upload/invoices"):
        r = client.get(path)
        assert r.status_code == 302 and "/abonnement-inactif" in _loc(r), path
    r = client.post("/facturation/nouvelle", data={"csrf_token": "csrf-test"})
    assert r.status_code == 302 and "/abonnement-inactif" in _loc(r)
    # Exports en lecture seule toujours disponibles (RGPD / conservation comptable).
    r = client.get("/facturation/achats/export")
    assert "/abonnement-inactif" not in _loc(r)
    r = client.get("/settings/gdpr-export")
    assert "/abonnement-inactif" not in _loc(r)
    # Payer reste possible.
    assert client.get("/billing").status_code == 200


def test_requires_feature_checks_subscription_status(billing_env):
    from profitos.feature_access import requires_feature

    @requires_feature("accounting_core")
    def view():
        return "ok"

    oid = make_org(plan="PRO", status="CANCELED", trial_days=None)
    owner = make_user(oid)
    with billing_env.app.test_request_context("/x"):
        from flask import session
        session["user_id"] = owner; session["org_id"] = oid; session["role"] = "OWNER"
        resp = view()
        assert resp != "ok" and "/abonnement-inactif" in resp.headers["Location"]


def test_expired_trial_keeps_trial_plan_but_paid_plan_helper_blocks(billing_env):
    from profitos.feature_access import current_plan_is_paid
    oid = make_org(trial_days=-2)
    owner = make_user(oid)
    with billing_env.app.test_request_context("/x"):
        from flask import session
        session["user_id"] = owner; session["org_id"] = oid
        assert current_plan_is_paid() is False
    oid2 = make_org(trial_days=3)
    with billing_env.app.test_request_context("/x"):
        from flask import session
        session["user_id"] = owner; session["org_id"] = oid2
        assert current_plan_is_paid() is True


def test_api_v1_is_blocked_for_inactive_subscription_and_starter(billing_env):
    raw = runtime.generate_api_key()
    oid = make_org(plan="PRO", status="CANCELED", trial_days=None)
    c = runtime.auth_cx()
    c.execute("INSERT INTO api_keys(organization_id,key_hash,key_prefix,created_at,scope,scopes) VALUES(?,?,?,?,?,?)",
              (oid, runtime.hash_api_key(raw), raw[:12], runtime.now(), "read", "read"))
    c.commit(); c.close()
    client = billing_env.app.test_client()
    r = client.get("/api/v1/summary", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 402
    assert r.get_json()["error"] == "subscription_inactive"
    c = runtime.auth_cx()
    c.execute("UPDATE organizations SET plan='STARTER',status='ACTIVE_PAID' WHERE id=?", (oid,))
    c.commit(); c.close()
    r = client.get("/api/v1/summary", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 403


def test_feature_mapping_matches_pricing_features():
    from profitos.feature_access import feature_for_request, FEATURE_PATH_PREFIXES
    from profitos.plan_limits import FEATURE_LABELS
    assert feature_for_request("/comptabilite/paie", "payroll_imports_list") == "accounting_core"
    assert feature_for_request("/settings/swan/transfer/sepa", "swan_sepa_transfer") == "banking"
    assert feature_for_request("/portfolio", "portfolio") == "multi_entity"
    assert feature_for_request("/cabinet/portefeuille", "cabinet_portfolio") == "multi_entity"
    assert feature_for_request("/entites/basculer", "entity_switch") is None
    assert feature_for_request("/integrations/ecommerce", "ecommerce_settings") == "api"
    assert feature_for_request("/facturation", "invoicing_list") is None
    # Toute fonctionnalité contrôlée est affichée sur la page Tarifs.
    for _prefix, feature in FEATURE_PATH_PREFIXES:
        assert feature in FEATURE_LABELS
    assert not feature_enabled("STARTER", "api") and feature_enabled("PRO", "api")


def test_every_route_of_a_paid_module_is_mapped_to_a_feature(billing_env):
    """Garde par module (comme MODULE_AREAS) : aucune route d'emprunts, paie,
    révision, crédit-bail, analytique, cabinet, Swan, API... n'échappe au contrôle."""
    from profitos.feature_access import (feature_for_request, MODULE_FEATURES,
                                         FEATURE_EXEMPT_ENDPOINTS, SUBSCRIPTION_EXEMPT_ENDPOINTS)
    from profitos.plan_limits import FEATURE_LABELS
    app = billing_env.app
    assert set(MODULE_FEATURES.values()) <= set(FEATURE_LABELS)
    seen = set()
    with app.test_request_context("/"):
        for rule in app.url_map.iter_rules():
            mod = getattr(app.view_functions[rule.endpoint], "__module__", "")
            if mod not in MODULE_FEATURES or rule.endpoint in FEATURE_EXEMPT_ENDPOINTS | SUBSCRIPTION_EXEMPT_ENDPOINTS:
                continue
            assert feature_for_request(rule.rule, rule.endpoint) == MODULE_FEATURES[mod], rule.rule
            seen.add(mod)
    for mod in ("profitos.routes.loans", "profitos.routes.payroll", "profitos.routes.reviews",
                "profitos.routes.finance_leases", "profitos.routes.analytics", "profitos.routes.cabinet",
                "profitos.routes.swan_baas", "profitos.routes.ecommerce", "profitos.routes.entities"):
        assert mod in seen


def test_past_due_grace_period_then_blocked(billing_env):
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    from v4_billing_support import org_row
    a = make_org(plan="PRO", status="PAST_DUE", trial_days=None, past_due_since=recent)
    b = make_org(plan="PRO", status="PAST_DUE", trial_days=None, past_due_since=old)
    assert runtime.PAST_DUE_GRACE_DAYS == 7
    assert runtime.subscription_state(org_row(a)) == "PAST_DUE_GRACE"
    assert runtime.org_has_access(org_row(a)) is True
    assert runtime.subscription_state(org_row(b)) == "PAST_DUE_BLOCKED"
    assert runtime.org_has_access(org_row(b)) is False


def test_production_without_stripe_keeps_valid_trial_open(billing_env, monkeypatch):
    from v4_billing_support import org_row
    monkeypatch.setattr(runtime, "BILLING_ENABLED", False)
    monkeypatch.setenv("PROFITOS_ENV", "production")
    trial = make_org(trial_days=5)
    expired = make_org(trial_days=-1)
    paid = make_org(plan="MULTI", status="ACTIVE_PAID", trial_days=None)
    assert runtime.org_has_access(org_row(trial)) is True
    assert runtime.org_has_access(org_row(expired)) is False
    assert runtime.org_has_access(org_row(paid)) is True
    monkeypatch.setenv("PROFITOS_ENV", "development")
    assert runtime.org_has_access(org_row(expired)) is True  # confort de développement local
    # Lot C : PROFITOS_ENV absent ou inconnu = production (config.is_production),
    # jamais d'accès ouvert par défaut.
    for value in (None, "", "prod", "staging"):
        if value is None:
            monkeypatch.delenv("PROFITOS_ENV", raising=False)
        else:
            monkeypatch.setenv("PROFITOS_ENV", value)
        assert runtime.org_has_access(org_row(expired)) is False, value
        assert runtime.org_has_access(org_row(trial)) is True, value


def test_account_security_and_unsubscribe_stay_reachable_without_subscription(billing_env):
    oid = make_org(trial_days=-1)
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    assert "/abonnement-inactif" in _loc(client.get("/"))
    for path in ("/compte/securite", "/compte/mot-de-passe", "/compte/2fa", "/settings/gdpr-export"):
        r = client.get(path)
        assert "/abonnement-inactif" not in _loc(r), path


def test_trial_banner_promises_exactly_pro(billing_env):
    oid = make_org(trial_days=5)
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    html = client.get("/billing").get_data(as_text=True)
    assert "Toutes les fonctions de la formule Pro sont incluses pendant l'essai" in html
    assert "(5 utilisateurs, comptabilité, banque, API)" in html
    assert "nécessitent un abonnement" not in html


# --- Item 1 : Stripe obligatoire en production (preflight + render.yaml) ---

def test_preflight_production_requires_stripe(monkeypatch, capsys, tmp_path):
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    import cloud_preflight
    full = {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_WEBHOOK_SECRET": "whsec_x",
            "STRIPE_PRICE_PRO_ID": "price_x"}
    assert cloud_preflight.check_stripe(full) == []
    for missing in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_PRO_ID"):
        env = {k: v for k, v in full.items() if k != missing}
        errs = cloud_preflight.check_stripe(env)
        assert errs and "Stripe" in errs[0], missing
    # Ancien nom du tarif Pro encore accepté.
    legacy = {"STRIPE_SECRET_KEY": "sk", "STRIPE_WEBHOOK_SECRET": "wh", "STRIPE_PRICE_ID": "price_y"}
    assert cloud_preflight.check_stripe(legacy) == []
    # main() échoue avant toute migration quand seul Stripe manque.
    monkeypatch.setenv("PROFITOS_ENV", "production")
    monkeypatch.setenv("PROFITOS_SECRET_KEY", "x" * 40)
    monkeypatch.setenv("APP_BASE_URL", "https://app.profitos.fr")
    monkeypatch.setenv("PROFITOS_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("RESEND_API_KEY", "re_x")
    for k in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_ID", "STRIPE_PRICE_STARTER_ID",
              "STRIPE_PRICE_PRO_ID", "STRIPE_PRICE_BUSINESS_ID", "STRIPE_PRICE_MULTI_ID"):
        monkeypatch.delenv(k, raising=False)
    from profitos import db as dbmod
    monkeypatch.setattr(dbmod, "database_url_configured", lambda: True)
    assert cloud_preflight.main([]) == 1
    out = capsys.readouterr().out
    assert "STRIPE_SECRET_KEY" in out and "sk_test" not in out


def test_render_web_service_declares_stripe_after_database_url():
    from pathlib import Path
    import yaml
    root = Path(__file__).resolve().parents[1]
    bp = yaml.safe_load((root / "render.yaml").read_text(encoding="utf-8"))
    web = next(s for s in bp["services"] if s["type"] == "web")
    keys = [e.get("key") for e in web["envVars"]]
    for k in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PUBLISHABLE_KEY",
              "STRIPE_PRICE_STARTER_ID", "STRIPE_PRICE_PRO_ID", "STRIPE_PRICE_BUSINESS_ID",
              "STRIPE_PRICE_MULTI_ID"):
        entry = next(e for e in web["envVars"] if e.get("key") == k)
        assert entry.get("sync") is False and "value" not in entry
        assert keys.index(k) > keys.index("DATABASE_URL")
    assert {"fromGroup": "profitos-shared"} in web["envVars"]


def test_billing_page_without_stripe_shows_plan_and_keeps_trial_open(billing_env, monkeypatch):
    from profitos.routes import account
    for mod in (runtime, account):
        monkeypatch.setattr(mod, "BILLING_ENABLED", False)
        monkeypatch.setattr(mod, "get_stripe", lambda: None)
    monkeypatch.setenv("PROFITOS_ENV", "production")
    paid = make_org(plan="MULTI", status="ACTIVE_PAID", trial_days=None)
    owner = make_user(paid)
    client = billing_env.app.test_client()
    login(client, owner, paid)
    html = client.get("/billing").get_data(as_text=True)
    assert "Formule Multi-sociétés — active" in html
    assert "Facturation indisponible" not in html
    assert "Paiement en ligne bientôt disponible" in html
    trial = make_org(name="Essai", trial_days=6)
    t_owner = make_user(trial)
    client2 = billing_env.app.test_client()
    login(client2, t_owner, trial)
    html = client2.get("/billing").get_data(as_text=True)
    assert "Essai gratuit — 6 jours restants" in html and "Votre essai gratuit continue normalement" in html
    assert "/abonnement-inactif" not in _loc(client2.get("/"))

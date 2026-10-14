from functools import wraps

from flask import flash, g, jsonify, redirect, request, session, url_for

from profitos.runtime import (
    auth_cx, current_org, current_role, effective_plan, hash_api_key,
    log_security_event, org_has_access, trial_is_active,
    billing_org,
)
from profitos.plan_limits import FEATURE_LABELS, feature_enabled, minimum_plan_for


PAID_PLANS = {"STARTER", "PRO", "BUSINESS", "MULTI"}


def upgrade_message(feature_name, needed_plan):
    """Message clair : quelle fonction, dans quelle formule, à quel prix."""
    from profitos.runtime import STRIPE_PLANS
    label = FEATURE_LABELS.get(feature_name, "Cette fonctionnalité")
    p = STRIPE_PLANS.get(needed_plan or "")
    if not p:
        return "Cette fonctionnalité n’est pas incluse dans votre formule actuelle."
    owner_hint = (" Passez à cette formule en un clic depuis la page Abonnement."
                  if current_role() == "OWNER" else
                  " Le propriétaire du compte peut changer de formule depuis la page Abonnement.")
    return (f"🔒 {label} : inclus à partir de la formule {p['name']} "
            f"({p['price_eur']} € HT / mois).{owner_hint}")


def is_paid_plan(plan):
    return (plan or "").strip().upper() in PAID_PLANS


def current_plan_is_paid():
    """True si l'organisation a un abonnement payant actif (plan payant +
    statut ACTIVE_PAID ou impayé encore en délai de grâce) OU un essai en cours.

    L'essai gratuit donne exactement les fonctions de la formule Pro : il n'est
    donc plus bloqué ici. Un essai expiré, un abonnement résilié ou un impayé
    hors délai de grâce restent bloqués.
    """
    org = current_org()
    if not org:
        return False
    payer = billing_org(org)
    plan = payer["plan"]
    status = payer["status"]
    if is_paid_plan(plan) and status == "ACTIVE_PAID":
        return True
    if is_paid_plan(plan) and status == "PAST_DUE":
        return org_has_access(org)
    return trial_is_active(payer) and org_has_access(org)


def _deny_target():
    """Destination sans boucle : la facturation pour le propriétaire, la page
    d'abonnement (accessible à tous les rôles) pour les autres."""
    return url_for("billing") if current_role() == "OWNER" else url_for("subscription_inactive", raison="formule")


def _deny_paid_feature(target="paid_plan"):
    org = current_org()
    log_security_event(
        "PAID_PLAN_REQUIRED",
        "BLOCKED",
        organization_id=org["id"] if org else None,
        target=target,
    )
    if not org_has_access(org):
        return redirect(url_for("subscription_inactive"))
    flash(
        "Cette fonctionnalité nécessite un abonnement actif (Starter, Pro, Business ou Multi-sociétés) "
        "ou un essai gratuit en cours."
    )
    return redirect(_deny_target())


def requires_paid_plan(fn):
    """Autorise STARTER, PRO, BUSINESS et MULTI actifs, ainsi qu'un essai en cours
    (essai = formule Pro). Essai expiré / abonnement inactif : bloqués."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_plan_is_paid() and not is_readonly_export_request():
            return _deny_paid_feature("paid_plan")
        return fn(*args, **kwargs)
    return wrapped


def requires_feature(feature_name):
    """Bloque côté serveur une fonctionnalité non incluse dans le plan courant,
    ou indisponible parce que l'abonnement n'est pas actif (statut vérifié)."""
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if is_readonly_export_request():
                return fn(*args, **kwargs)  # exports toujours disponibles (RGPD)
            org = current_org()
            if not org_has_access(org):
                log_security_event(
                    "SUBSCRIPTION_INACTIVE",
                    "BLOCKED",
                    organization_id=org["id"] if org else None,
                    target=feature_name,
                )
                return redirect(url_for("subscription_inactive"))
            plan = effective_plan(org) if org else "TRIAL"

            if not feature_enabled(plan, feature_name):
                log_security_event(
                    "PLAN_FEATURE_BLOCKED",
                    "BLOCKED",
                    organization_id=org["id"] if org else None,
                    target=feature_name,
                )
                needed = minimum_plan_for(feature_name)
                flash(upgrade_message(feature_name, needed))
                if current_role() == "OWNER":
                    # La page Abonnement met en avant la formule nécessaire, avec un
                    # bouton de changement immédiat.
                    return redirect(url_for("billing", formule=needed) + "#formules" if needed else url_for("billing"))
                return redirect(_deny_target())

            return fn(*args, **kwargs)
        return wrapped
    return decorator


# ---------------------------------------------------------------------------
# Garde global d'abonnement (before_request) : aucune route connectée ne doit
# échapper au contrôle d'abonnement ni au contrôle de formule.
# ---------------------------------------------------------------------------

# Routes toujours accessibles, même sans abonnement actif : payer, se
# déconnecter, gérer son compte, pages légales/publiques, santé, webhooks.
SUBSCRIPTION_EXEMPT_ENDPOINTS = {
    "static", "login", "logout", "signup", "verify_email", "verify_resend",
    "forgot_password", "reset_password",
    "billing", "billing_checkout", "billing_portal", "billing_success", "billing_webhook", "billing_change_plan",
    "toggle_ui_mode", "account_name_change",
    "subscription_inactive", "org_switch", "toggle_theme", "changelog_seen",
    "gdpr_export", "delete_account",
    "pricing", "features_overview", "request_demo", "public_profit_audit",
    "terms_of_use", "terms_of_sale", "legal_notice", "privacy_policy", "help_center",
    "healthz", "readyz", "ops_health", "service_worker",
    "public_invoice_view", "public_invoice_pdf", "public_invoice_status",
    "public_quote_view", "public_quote_accept", "public_quote_pdf", "public_quote_refuse",
    "supplier_inbox_webhook", "swan_webhook", "weinvoice_onboarding_webhook",
    "weinvoice_invoice_status_webhook",
    "mobile_csrf_token", "register_push_token", "unregister_push_token",
    # Sécurité du compte (lot C) : mot de passe, 2FA, email, données personnelles,
    # suppression programmée — jamais bloqués par l'abonnement.
    "account_security", "account_password_change", "account_email_change", "account_email_confirm",
    "two_factor_setup", "two_factor_recovery_codes", "two_factor_disable", "login_two_factor",
    "org_require_two_factor", "org_deletion_pending", "org_deletion_cancel", "personal_data_export",
    # Invitations (l'organisation d'accueil a son propre abonnement), désinscription
    # des emails et déclenchement interne des tâches planifiées (jeton dédié).
    "team_invitation", "accountant_invite_accept",
    "weekly_report_unsubscribe", "weekly_report_unsubscribe_one_click", "internal_job_trigger",
}

# Accessibles en lecture seule (GET) sans abonnement : consulter ses paramètres.
SUBSCRIPTION_READONLY_ENDPOINTS = {"settings"}

# Exports : toujours accessibles en lecture (RGPD, obligation de conservation
# comptable), même abonnement expiré ou après une baisse de formule.
EXPORT_ENDPOINTS = {
    "recover_export", "save_export", "grow_export", "purchase_export",
    "audit_export", "gdpr_export", "export_download", "personal_data_export",
    "accounting_fec_export", "accounting_fec_download",
    "invoicing_pdf", "invoicing_facturx", "invoicing_credit_pdf", "invoicing_quote_pdf",
    "delivery_note_pdf", "purchase_document_view", "cabinet_document_download",
}
# L'export FEC se prépare par un POST (génération du lien), sans modification.
EXPORT_POST_ENDPOINTS = {"accounting_fec_export"}

# Correspondance module -> fonctionnalité de formule (cf. pricing.html, qui
# affiche exactement ces fonctionnalités par formule via PLAN_LIMITS et
# FEATURE_LABELS). Même principe que MODULE_AREAS pour les rôles : toute route
# ajoutée à l'un de ces modules est contrôlée sans décorateur supplémentaire.
MODULE_FEATURES = {
    "profitos.routes.accounting": "accounting_core",
    "profitos.routes.loans": "accounting_core",
    "profitos.routes.finance_leases": "accounting_core",
    "profitos.routes.analytics": "accounting_core",
    "profitos.routes.reviews": "accounting_core",
    "profitos.routes.payroll": "accounting_core",
    "profitos.routes.ledger": "accounting_core",
    "profitos.routes.bank_sync": "banking",
    "profitos.routes.swan_baas": "banking",
    "profitos.routes.entities": "multi_entity",
    "profitos.routes.cabinet": "multi_entity",
    "profitos.routes.api": "api",
    "profitos.routes.ecommerce": "api",
    "profitos.routes.financial_brain": "advanced_ai",
    "profitos.routes.cash_intelligence": "advanced_ai",
    "profitos.routes.decision_simulator": "advanced_ai",
}
# Routes isolées hors de ces modules.
ENDPOINT_FEATURES = {
    "portfolio": "multi_entity",
}
# Filet de sécurité par préfixe d'URL (routes déclarées ailleurs).
FEATURE_PATH_PREFIXES = (
    ("/comptabilite/", "accounting_core"),
    ("/banking", "banking"),
    ("/settings/swan", "banking"),
    ("/entites", "multi_entity"),
    ("/portfolio", "multi_entity"),
    ("/cabinet/", "multi_entity"),
    ("/settings/api-keys", "api"),
    ("/settings/webhooks", "api"),
    ("/integrations/ecommerce", "api"),
    ("/api/v1/", "api"),
    ("/financial-brain", "advanced_ai"),
    ("/cash-intelligence", "advanced_ai"),
    ("/decision-simulator", "advanced_ai"),
)
# Endpoints d'un module contrôlé qui restent libres (revenir à la société mère,
# accepter une invitation de collaboration comptable, webhooks signés).
FEATURE_EXEMPT_ENDPOINTS = {"entity_switch", "accountant_invite_accept", "swan_webhook"}


def feature_for_request(path=None, endpoint=None):
    path = path if path is not None else request.path
    endpoint = endpoint if endpoint is not None else request.endpoint
    if endpoint in FEATURE_EXEMPT_ENDPOINTS:
        return None
    if endpoint in ENDPOINT_FEATURES:
        return ENDPOINT_FEATURES[endpoint]
    from flask import current_app, has_app_context
    if endpoint and has_app_context():
        fn = current_app.view_functions.get(endpoint)
        feature = MODULE_FEATURES.get(getattr(fn, "__module__", ""))
        if feature:
            return feature
    for prefix, feature in FEATURE_PATH_PREFIXES:
        if path == prefix.rstrip("/") or path.startswith(prefix):
            return feature
    return None


def is_readonly_export_request():
    """Export de données en lecture seule : jamais bloqué par l'abonnement ou la
    formule (portabilité RGPD, obligation de conservation comptable)."""
    ep = request.endpoint
    if ep not in EXPORT_ENDPOINTS:
        return False
    return request.method in ("GET", "HEAD") or ep in EXPORT_POST_ENDPOINTS


def _api_guard():
    """API publique /api/v1 : même contrôle d'abonnement et de formule que le web."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None  # la vue répond 401
    c = auth_cx()
    try:
        row = c.execute(
            "SELECT o.* FROM api_keys k JOIN organizations o ON o.id=k.organization_id "
            "WHERE k.key_hash=? AND k.revoked_at IS NULL",
            (hash_api_key(auth_header[7:].strip()),),
        ).fetchone()
    finally:
        c.close()
    if not row:
        return None
    if not org_has_access(row):
        return jsonify({"error": "subscription_inactive",
                        "message": "Abonnement ProfitOS inactif : l'API est suspendue."}), 402
    if not feature_enabled(effective_plan(row), "api"):
        return jsonify({"error": "plan_feature_unavailable",
                        "message": "L'API n'est pas incluse dans votre formule actuelle."}), 403
    return None


def enforce_subscription_access():
    endpoint = request.endpoint
    if endpoint is None or endpoint in SUBSCRIPTION_EXEMPT_ENDPOINTS:
        return None
    if request.path.startswith("/api/v1/"):
        return _api_guard()
    if not session.get("user_id") or not session.get("org_id"):
        return None  # anonyme : login_required s'en charge
    org = current_org()
    if not org:
        return None
    if is_readonly_export_request():
        return None
    if not org_has_access(org):
        if endpoint in SUBSCRIPTION_READONLY_ENDPOINTS and request.method in ("GET", "HEAD"):
            return None
        if not getattr(g, "_subscription_block_logged", False):
            g._subscription_block_logged = True
            log_security_event("SUBSCRIPTION_INACTIVE", "BLOCKED",
                               organization_id=org["id"], target=endpoint)
        if request.accept_mimetypes.best == "application/json" or request.is_json:
            return jsonify({"error": "subscription_inactive",
                            "message": "Abonnement inactif."}), 402
        return redirect(url_for("subscription_inactive"))
    feature = feature_for_request()
    if feature and not feature_enabled(effective_plan(org), feature):
        log_security_event("PLAN_FEATURE_BLOCKED", "BLOCKED",
                           organization_id=org["id"], target=feature)
        needed = minimum_plan_for(feature)
        flash(upgrade_message(feature, needed))
        if current_role() == "OWNER" and needed:
            return redirect(url_for("billing", formule=needed) + "#formules")
        return redirect(_deny_target())
    return None

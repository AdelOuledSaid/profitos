from functools import wraps

from flask import flash, redirect, url_for

from profitos.runtime import current_org, log_security_event
from profitos.plan_limits import feature_enabled


PAID_PLANS = {"STARTER", "PRO", "BUSINESS"}


def is_paid_plan(plan):
    return (plan or "").strip().upper() in PAID_PLANS


def current_plan_is_paid():
    org = current_org()
    return bool(
        org
        and is_paid_plan(org["plan"])
        and org["status"] == "ACTIVE_PAID"
    )


def _deny_paid_feature(target="paid_plan"):
    org = current_org()
    log_security_event(
        "PAID_PLAN_REQUIRED",
        "BLOCKED",
        organization_id=org["id"] if org else None,
        target=target,
    )
    flash(
        "Cette fonctionnalité nécessite un abonnement Starter, Pro ou Business. "
        "Choisissez une formule depuis la page Facturation."
    )
    return redirect(url_for("billing"))


def requires_paid_plan(fn):
    """Autorise uniquement STARTER, PRO et BUSINESS. TRIAL/FREE sont bloqués."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_plan_is_paid():
            return _deny_paid_feature("paid_plan")
        return fn(*args, **kwargs)
    return wrapped


def requires_feature(feature_name):
    """Bloque côté serveur une fonctionnalité non incluse dans le plan courant."""
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            org = current_org()
            plan = org["plan"] if org else "TRIAL"

            if not feature_enabled(plan, feature_name):
                log_security_event(
                    "PLAN_FEATURE_BLOCKED",
                    "BLOCKED",
                    organization_id=org["id"] if org else None,
                    target=feature_name,
                )
                flash(
                    "Cette fonctionnalité est disponible avec les formules Pro et Business. "
                    "Vous pouvez changer de formule depuis la page Facturation."
                )
                return redirect(url_for("billing"))

            return fn(*args, **kwargs)
        return wrapped
    return decorator

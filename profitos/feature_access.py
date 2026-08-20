from functools import wraps

from flask import flash, redirect, url_for

from profitos.runtime import current_org, log_security_event
from profitos.plan_limits import feature_enabled


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

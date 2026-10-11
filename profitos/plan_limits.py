"""Limites commerciales centralisées par plan ProfitOS.

None signifie "illimité".
Les contrôles doivent toujours être faits côté serveur.
"""

PLAN_LIMITS = {
    "TRIAL": {
        "team_members": 2, "organizations": 1, "imports_per_month": 20,
        "reports_per_month": 10, "advanced_features": False,
        "accounting_core": False, "banking": False, "advanced_ai": False, "multi_entity": False,
    },
    "STARTER": {
        "team_members": 2, "organizations": 1, "imports_per_month": 20,
        "reports_per_month": 10, "advanced_features": False,
        "accounting_core": False, "banking": False, "advanced_ai": False, "multi_entity": False,
    },
    "PRO": {
        "team_members": 5, "organizations": 1, "imports_per_month": 200,
        "reports_per_month": 100, "advanced_features": True,
        "accounting_core": True, "banking": True, "advanced_ai": False, "multi_entity": False,
    },
    "BUSINESS": {
        "team_members": 20, "organizations": 1, "imports_per_month": None,
        "reports_per_month": None, "advanced_features": True,
        "accounting_core": True, "banking": True, "advanced_ai": True, "multi_entity": False,
    },
    "MULTI": {
        "team_members": 100, "organizations": 25, "imports_per_month": None,
        "reports_per_month": None, "advanced_features": True,
        "accounting_core": True, "banking": True, "advanced_ai": True, "multi_entity": True,
    },
}


def normalize_plan(plan):
    p=(plan or "TRIAL").strip().upper()
    return p if p in PLAN_LIMITS else "TRIAL"


def plan_limits(plan):
    return PLAN_LIMITS[normalize_plan(plan)]


def plan_limit(plan, key):
    return plan_limits(plan).get(key)


def within_limit(plan, key, current_count, additional=1):
    """True si l'ajout demandé reste dans le quota.

    Une limite à None est illimitée.
    """
    limit=plan_limit(plan,key)
    if limit is None:
        return True
    try:
        current=int(current_count)
        add=int(additional)
    except (TypeError,ValueError):
        return False
    return current+add <= int(limit)


def feature_enabled(plan, feature):
    return bool(plan_limits(plan).get(feature,False))

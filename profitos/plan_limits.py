"""Limites commerciales centralisées par plan ProfitOS.

None signifie "illimité".
Les contrôles doivent toujours être faits côté serveur.

Règle produit : l'essai gratuit (TRIAL) donne exactement les fonctionnalités
et capacités de la formule Pro ; seul le quota d'extractions IA est réduit.
"""

# Quota mensuel d'extractions de documents par IA (factures d'achat, reçus,
# relevés scannés, synchronisation cloud, boîte mail fournisseurs).
# Source de vérité unique pour tous les plans.
AI_EXTRACTIONS_PER_MONTH = {
    "TRIAL": 20,
    "STARTER": 30,
    "PRO": 200,
    "BUSINESS": 1000,
    "MULTI": 3000,
}

PLAN_LIMITS = {
    "STARTER": {
        "team_members": 2, "organizations": 1, "imports_per_month": 20,
        "reports_per_month": 10, "advanced_features": False,
        "accounting_core": False, "banking": False, "advanced_ai": False, "multi_entity": False,
        "api": False,
    },
    "PRO": {
        "team_members": 5, "organizations": 1, "imports_per_month": 200,
        "reports_per_month": 100, "advanced_features": True,
        "accounting_core": True, "banking": True, "advanced_ai": False, "multi_entity": False,
        "api": True,
    },
    "BUSINESS": {
        "team_members": 20, "organizations": 1, "imports_per_month": None,
        "reports_per_month": None, "advanced_features": True,
        "accounting_core": True, "banking": True, "advanced_ai": True, "multi_entity": False,
        "api": True,
    },
    "MULTI": {
        "team_members": 100, "organizations": 25, "imports_per_month": None,
        "reports_per_month": None, "advanced_features": True,
        "accounting_core": True, "banking": True, "advanced_ai": True, "multi_entity": True,
        "api": True,
    },
}
# L'essai gratuit reprend exactement la formule Pro.
PLAN_LIMITS["TRIAL"] = dict(PLAN_LIMITS["PRO"])
for _plan, _quota in AI_EXTRACTIONS_PER_MONTH.items():
    PLAN_LIMITS[_plan]["ai_extractions_per_month"] = _quota

# Libellés commerciaux des fonctionnalités contrôlées côté serveur, repris par
# les pages Tarifs et Facturation : ce qui est affiché = ce qui est appliqué.
FEATURE_LABELS = {
    "accounting_core": "Comptabilité, TVA, FEC, paie, immobilisations, emprunts, crédit-bail, révision et analytique",
    "banking": "Banque, rapprochement et Compte Pro (virements SEPA)",
    "api": "API, webhooks et connecteurs e-commerce",
    "advanced_features": "Actualisation BOAMP, envoi email automatique et analyse DCE",
    "advanced_ai": "Analyse financière avancée (règles d'analyse) : diagnostic financier, trésorerie & scénarios, simulateur de décision",
    "multi_entity": "Multi-sociétés : entités, consolidation, portefeuille et cabinet",
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

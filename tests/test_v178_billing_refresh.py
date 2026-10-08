from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def test_v178_billing_surfaces_current_financial_modules():
    t=(ROOT/'templates'/'billing.html').read_text(encoding='utf-8')
    for x in ['Gisements de cash','Diagnostic financier','trésorerie','Simulateur de décision','Flux projetés','suivi des marges']:
        assert x in t


def test_v178_billing_preserves_current_plan_contract_and_stripe_actions():
    t=(ROOT/'templates'/'billing.html').read_text(encoding='utf-8')
    # Pass 43 contract: four commercial tiers and server-backed Stripe actions.
    for x in ['Starter','9 €','Pro','19 €','Business','49 €','Multi-Entity','199 €']:
        assert x in t
    for x in ['Comptabilité','Banque','Diagnostic financier','Simulateur de décision','Multi-entités']:
        assert x in t
    assert "url_for('billing_checkout')" in t
    assert "url_for('billing_portal')" in t


def test_v178_public_pricing_is_consistent_with_pass43():
    t=(ROOT/'templates'/'pricing.html').read_text(encoding='utf-8')
    # Public page must expose all four tiers and the current entitlement split.
    for x in ['Starter','9 €','Pro','19 €','Business','49 €','Multi-Entity','199 €',
              'IA financière avancée','Multi-entités','Demander une démo',"14 jours d'essai gratuit"]:
        assert x in t

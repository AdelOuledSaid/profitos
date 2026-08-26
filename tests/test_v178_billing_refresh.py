from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_v178_billing_surfaces_new_financial_modules():
    t=(ROOT/'templates'/'billing.html').read_text(encoding='utf-8')
    for x in ['Money Hunter','Financial Brain','Cash Intelligence','AI CFO Planner','Cash Forecast','Margin Watch']:
        assert x in t

def test_v178_billing_preserves_plan_contract_and_stripe_actions():
    t=(ROOT/'templates'/'billing.html').read_text(encoding='utf-8')
    for x in ['20 imports / mois','10 rapports / mois','200 imports / mois','100 rapports / mois','Imports illimités','Rapports illimités','Actualisation BOAMP','Envoi email automatique','Analyse DCE']:
        assert x in t
    assert "url_for('billing_checkout')" in t
    assert "url_for('billing_portal')" in t

def test_v178_public_pricing_is_consistent():
    t=(ROOT/'templates'/'pricing.html').read_text(encoding='utf-8')
    for x in ['Financial Brain','Cash Intelligence','AI CFO Planner','Demander une démo','14 jours d\'essai gratuit']:
        assert x in t

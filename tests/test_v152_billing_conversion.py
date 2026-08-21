from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def test_billing_has_clear_plan_comparison():
    t=(ROOT/'templates'/'billing.html').read_text(encoding='utf-8')
    assert 'RECOMMANDÉ' in t
    assert 'Tout Starter' in t
    assert 'Tout Pro' in t
    assert 'Paiement sécurisé par Stripe' in t


def test_billing_lists_real_plan_limits():
    t=(ROOT/'templates'/'billing.html').read_text(encoding='utf-8')
    for s in ['20 imports / mois','10 rapports / mois','200 imports / mois','100 rapports / mois','Imports illimités','Rapports illimités']:
        assert s in t


def test_billing_lists_advanced_features():
    t=(ROOT/'templates'/'billing.html').read_text(encoding='utf-8')
    assert 'Actualisation BOAMP' in t
    assert 'Envoi email automatique' in t
    assert 'Analyse DCE' in t


def test_paid_customer_still_gets_stripe_portal():
    t=(ROOT/'templates'/'billing.html').read_text(encoding='utf-8')
    assert "url_for('billing_portal')" in t
    assert 'Gérer mon abonnement' in t

from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def test_trial_nav_hides_paid_sections():
    t=(ROOT/'templates'/'base.html').read_text(encoding='utf-8')
    # V4 : l'essai = formule Pro, et MULTI inclut tout.
    assert "paid_plan = auth_org and auth_org.plan in ('TRIAL','STARTER','PRO','BUSINESS','MULTI')" in t
    assert "paid_plan and can_access('grow')" in t
    assert "paid_plan and can_access('actions')" in t
    assert "paid_plan and can_access('uploads')" in t


def test_company_remains_visible():
    t=(ROOT/'templates'/'base.html').read_text(encoding='utf-8')
    assert "url_for('company')" in t
    assert "Profil entreprise" in t


def test_grow_refresh_is_pro_business_only_in_ui():
    t=(ROOT/'templates'/'grow.html').read_text(encoding='utf-8')
    assert "advanced_plan = auth_org and plan_feature_enabled(auth_org.plan,'advanced_features')" in t
    assert 'Actualiser BOAMP' in t
    assert 'Actualisation BOAMP · Pro' in t


def test_action_email_is_pro_business_only_in_ui():
    t=(ROOT/'templates'/'actions.html').read_text(encoding='utf-8')
    assert "advanced_plan = auth_org and plan_feature_enabled(auth_org.plan,'advanced_features')" in t
    assert 'Envoyer par e-mail' in t
    assert 'Envoi e-mail · Pro' in t

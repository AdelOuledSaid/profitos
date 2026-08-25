from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_v174_version():
    assert 'APP_VERSION = "1.7.4"' in (ROOT/'profitos/config.py').read_text(encoding='utf-8')

def test_before_after_decision_intelligence_ui():
    t=(ROOT/'templates/decision_simulator.html').read_text(encoding='utf-8')
    for x in ['SCÉNARIO INITIAL · SANS OPTIMISATION','PLAN OPTIMISÉ · BEFORE / AFTER INTELLIGENCE · V1.7.4','AVANT OPTIMISATION','APRÈS OPTIMISATION','BESOIN RÉDUIT DE','SOUTENABLE SOUS VOS CONTRAINTES']:
        assert x in t

def test_professional_landing_features():
    t=(ROOT/'templates/landing.html').read_text(encoding='utf-8')
    for x in ['FINANCIAL OPERATING SYSTEM POUR LE BTP','Recouvrement intelligent','Économies et marge','Prévision de trésorerie','Décisions financières simulées','Une priorité claire','Développement commercial','BEFORE / AFTER DECISION INTELLIGENCE','Action Center','Financial Brain','Cash Intelligence','AI CFO PLANNER']:
        assert x in t

def test_landing_has_conversion_ctas():
    t=(ROOT/'templates/landing.html').read_text(encoding='utf-8')
    assert "url_for('signup')" in t
    assert "url_for('request_demo')" in t
    assert "url_for('pricing')" in t

def test_v174_css_cache():
    t=(ROOT/'templates/base.html').read_text(encoding='utf-8')
    # Le versionnage manuel ('v=1740') a été remplacé par un cache-busting automatique
    # basé sur la date de modification du fichier (voir profitos.runtime.asset_url).
    assert "asset_url('financial-brain.css')" in t
    l=(ROOT/'templates/landing.html').read_text(encoding='utf-8')
    assert "asset_url('landing.css')" in l

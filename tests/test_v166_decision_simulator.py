from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_v166_version():
    assert 'APP_VERSION = "1.7.5"' in (ROOT/'profitos/config.py').read_text(encoding='utf-8')

def test_decision_simulator_registered():
    t=(ROOT/'profitos/__init__.py').read_text(encoding='utf-8')
    assert 'decision_simulator.register(app)' in t

def test_decision_simulator_navigation():
    t=(ROOT/'templates/base.html').read_text(encoding='utf-8')
    assert "url_for('decision_simulator')" in t
    assert "asset_url('financial-brain.css')" in t

def test_decision_simulator_route_and_model():
    t=(ROOT/'profitos/routes/decision_simulator.py').read_text(encoding='utf-8')
    assert "@app.route('/decision-simulator')" in t
    assert 'def _simulate_decision' in t
    assert "level='RISQUÉ'" in t
    assert 'financing_gap' in t

def test_decision_simulator_template_is_explainable():
    t=(ROOT/'templates/decision_simulator.html').read_text(encoding='utf-8')
    # "VERDICT FINANCIER" a été reformulé en "SCÉNARIO INITIAL · SANS OPTIMISATION"
    # (le panneau de verdict initial existe toujours, juste avec un intitulé plus clair).
    assert 'SCÉNARIO INITIAL · SANS OPTIMISATION' in t
    assert 'POINT BAS · AVANT' in t
    assert 'POINT BAS · APRÈS' in t
    assert "Les coûts, taxes ou revenus non renseignés ne sont pas inventés." in t

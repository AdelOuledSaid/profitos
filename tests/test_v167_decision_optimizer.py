from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_v167_version():
    assert 'APP_VERSION = "1.6.7"' in (ROOT/'profitos/config.py').read_text(encoding='utf-8')

def test_optimizer_engine_present():
    t=(ROOT/'profitos/routes/decision_simulator.py').read_text(encoding='utf-8')
    assert 'def _optimize_decision' in t
    assert 'installment_options' in t
    assert "reserve=5000.0" in t

def test_optimizer_ui_present():
    t=(ROOT/'templates/decision_simulator.html').read_text(encoding='utf-8')
    assert 'DECISION OPTIMIZER · V1.6.7' in t
    assert 'STRATÉGIE RECOMMANDÉE' in t
    assert 'Réserve minimale à préserver' in t
    assert 'FINANCEMENT À SÉCURISER' in t

def test_optimizer_cache_bumped():
    t=(ROOT/'templates/base.html').read_text(encoding='utf-8')
    assert "financial-brain.css',v='1670'" in t
    assert '>Decision Optimizer<' in t

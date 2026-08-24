from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_version_168():
    assert 'APP_VERSION = "1.6.8"' in (ROOT/'profitos/config.py').read_text(encoding='utf-8')

def test_planner_ui():
    t=(ROOT/'templates/decision_simulator.html').read_text(encoding='utf-8')
    assert 'AUTONOMOUS FINANCIAL PLANNER · V1.6.8' in t
    assert 'PLAN SANS FINANCEMENT' in t
    assert 'no_financing' in t

def test_planner_engine_present():
    t=(ROOT/'profitos/routes/decision_simulator.py').read_text(encoding='utf-8')
    assert 'def _strategy_label' in t
    assert "'no_financing':no_financing" in t
    assert 'list(range(decision_day,91,5))' in t

def test_cache_1680():
    t=(ROOT/'templates/base.html').read_text(encoding='utf-8')
    assert "financial-brain.css',v='1680'" in t

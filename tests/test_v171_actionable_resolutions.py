from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_v171_actionable_resolution_engine():
    r=(ROOT/'profitos/routes/decision_simulator.py').read_text(encoding='utf-8')
    assert "'target_amount'" in r
    assert "'target_max_financing'" in r
    assert 'Augmenter le plafond de financement de' in r

def test_v171_actionable_resolution_ui():
    t=(ROOT/'templates/decision_simulator.html').read_text(encoding='utf-8')
    assert 'FINANCIAL CONSISTENCY GUARD · V1.7.6' in t
    assert 'Tester cette stratégie' in t
    assert 'option.target_amount' in t
    assert 'option.target_max_financing' in t

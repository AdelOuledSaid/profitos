from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_v169_version():
    assert 'APP_VERSION = "1.7.1"' in (ROOT/'profitos/config.py').read_text(encoding='utf-8')

def test_v169_ui():
    t=(ROOT/'templates/decision_simulator.html').read_text(encoding='utf-8')
    for x in ['AI CFO · EXPLAINABLE PLANNER · V1.7.1','Financement maximum accepté','Décision au plus tard','TOP 3 · PLANS EXPLIQUÉS','AI CFO · QUESTION SUR CE SCÉNARIO']:
        assert x in t

def test_v169_engine_contract():
    t=(ROOT/'profitos/routes/decision_simulator.py').read_text(encoding='utf-8')
    for x in ['max_financing=None','deadline=90','top3','_cfo_answer','constraints_met']:
        assert x in t

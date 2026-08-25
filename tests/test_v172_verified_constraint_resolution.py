from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_v172_verified_engine_present():
    t=(ROOT/'profitos/routes/decision_simulator.py').read_text(encoding='utf-8')
    for x in ['def verify(','Solution vérifiée','Combinaison vérifiée','verified_minimum','verified_financing']:
        assert x in t

def test_v172_ui():
    t=(ROOT/'templates/decision_simulator.html').read_text(encoding='utf-8')
    assert 'FINANCIAL CONSISTENCY GUARD · V1.7.5' in t
    assert 'Vérifiée par le moteur' in t

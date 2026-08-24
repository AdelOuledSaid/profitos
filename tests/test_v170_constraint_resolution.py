from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_v170_version():
    assert 'APP_VERSION = "1.7.2"' in (ROOT/'profitos/config.py').read_text(encoding='utf-8')

def test_constraint_resolution_engine_present():
    t=(ROOT/'profitos/routes/decision_simulator.py').read_text(encoding='utf-8')
    for x in ['_build_constraint_resolutions','Augmenter le financement disponible','Réduire le montant de la décision','Partager l’effort']:
        assert x in t
    assert "simulation['resolutions']=_build_constraint_resolutions" in t

def test_constraint_resolution_ui():
    t=(ROOT/'templates/decision_simulator.html').read_text(encoding='utf-8')
    assert 'CONSTRAINT RESOLUTION ENGINE · V1.7.2' in t
    assert '3 stratégies pour rendre la décision possible' in t
    assert "Aucun revenu futur n'est inventé" in t

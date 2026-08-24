from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def test_version_1691():
    assert 'APP_VERSION = "1.7.4"' in (ROOT/'profitos/config.py').read_text(encoding='utf-8')

def test_optimizer_deduplicates_equivalent_plans():
    t=(ROOT/'profitos/routes/decision_simulator.py').read_text(encoding='utf-8')
    assert "Collapse financially equivalent plans" in t
    assert "equivalent_dates" in t
    assert "date la plus proche" in t

def test_optimizer_strict_financing_constraint_message():
    t=(ROOT/'profitos/routes/decision_simulator.py').read_text(encoding='utf-8')
    assert "Aucun plan compatible avec vos contraintes" in t
    assert "Écart :" in t
    assert "'constraint_gap'" in t

def test_template_distinguishes_incompatible_constraints():
    t=(ROOT/'templates/decision_simulator.html').read_text(encoding='utf-8')
    assert "Aucun plan compatible avec vos contraintes." in t
    assert "PLAN LE PLUS PROCHE" in t
    assert "constraint_gap" in t

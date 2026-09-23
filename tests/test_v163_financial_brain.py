from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_financial_brain_route_registered():
    t=(ROOT/'profitos'/'__init__.py').read_text(encoding='utf-8')
    assert 'financial_brain' in t
    assert 'financial_brain.register(app)' in t

def test_financial_brain_is_explainable():
    t=(ROOT/'profitos'/'routes'/'financial_brain.py').read_text(encoding='utf-8')
    assert 'def build_financial_brain()' in t
    assert "'capital_at_risk'" in t
    assert "'control_score'" in t
    assert "'best_decision'" in t
    assert "'data_gaps'" in t

def test_financial_brain_does_not_invent_cash():
    t=(ROOT/'profitos'/'routes'/'financial_brain.py').read_text(encoding='utf-8')
    # Financial Brain doit lire le solde persistant partagé, jamais en inventer un.
    # Depuis la session 23/09/2026 (multi-entités), la lecture passe par
    # get_cash_balance() plutôt que par du SQL en dur ciblant uniquement la
    # société mère — cette fonction lit toujours un solde réellement persisté
    # (financial_settings pour la société mère, entity_financial_settings pour
    # une filiale), jamais inventé, et est elle-même testée dans profitos/entities.py.
    assert "financial_settings=get_cash_balance(c,eid)" in t
    assert "from profitos.entities import get_cash_balance" in t
    assert "cash_balance=None if not financial_settings or financial_settings['cash_balance'] is None" in t
    assert "if cash_balance is None" in t
    assert 'Solde bancaire actuel non renseigné.' in t
    assert "decision':'À QUALIFIER'" in t

def test_financial_brain_template_has_core_sections():
    t=(ROOT/'templates'/'financial_brain.html').read_text(encoding='utf-8')
    for marker in ['FINANCIAL CONTROL SCORE','CAPITAL À RISQUE','NEXT BEST FINANCIAL DECISION','GROW · FINANCIAL CHECK','QUALITÉ DES DONNÉES']:
        assert marker in t

def test_financial_brain_in_navigation():
    t=(ROOT/'templates'/'base.html').read_text(encoding='utf-8')
    assert "url_for('financial_brain')" in t
    assert "asset_url('financial-brain.css')" in t

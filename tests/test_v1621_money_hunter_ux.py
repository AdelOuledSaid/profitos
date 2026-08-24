from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_money_hunter_has_human_risk_labels():
    t=(ROOT/'profitos/routes/money_hunter.py').read_text(encoding='utf-8')
    assert "risk_level = 'ÉLEVÉ'" in t
    assert "show_risk_score = evidence != 'INSUFFISANT'" in t
    assert "'payment_risk_level': risk_level" in t

def test_money_hunter_hides_unreliable_numeric_risk_in_ui():
    t=(ROOT/'templates/money_hunter.html').read_text(encoding='utf-8')
    assert "Historique insuffisant pour une estimation fiable" in t
    assert 'r.show_risk_score' in t
    assert 'Voir l’analyse détaillée' in t
    assert 'ACTION RECOMMANDÉE' in t

def test_money_hunter_ux_has_no_inline_event_handlers():
    import re
    t=(ROOT/'templates/money_hunter.html').read_text(encoding='utf-8')
    assert not re.search(r'\son[a-z]+\s*=',t,re.I)

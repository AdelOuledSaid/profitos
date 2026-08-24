from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_money_hunter_polish_classes_present():
    t=(ROOT/'templates/money_hunter.html').read_text(encoding='utf-8')
    assert 'money-kind type' in t
    assert 'recommendation-text' in t

def test_money_hunter_polish_css_present():
    t=(ROOT/'static/style.css').read_text(encoding='utf-8')
    assert 'V1.6.2.1 polish — Money Hunter decision cards' in t
    assert '.money-badges .money-kind' in t
    assert '.recommendation-text' in t
    assert '.money-decision-value .money-amount' in t

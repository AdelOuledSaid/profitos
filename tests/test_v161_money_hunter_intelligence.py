from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def test_v161_priority_score_is_explainable():
    t=(ROOT/'profitos'/'routes'/'money_hunter.py').read_text(encoding='utf-8')
    assert 'def _intelligence(' in t
    assert "'priority_score': priority_score" in t
    assert "'urgency_level': urgency_level" in t
    assert "'why': why" in t


def test_v161_today_actions_exist():
    t=(ROOT/'profitos'/'routes'/'money_hunter.py').read_text(encoding='utf-8')
    assert 'today_actions' in t
    page=(ROOT/'templates'/'money_hunter.html').read_text(encoding='utf-8')
    assert 'À FAIRE AUJOURD’HUI' in page
    assert 'Priority Score' in page
    assert 'Pourquoi :' in page


def test_v161_no_fake_grow_money():
    t=(ROOT/'profitos'/'routes'/'money_hunter.py').read_text(encoding='utf-8')
    assert "'expected': 0.0" in t
    assert 'Sans valeur de marché fiable' in t


def test_v161_version():
    t=(ROOT/'profitos'/'config.py').read_text(encoding='utf-8')
    assert 'APP_VERSION = "1.7.0"' in t


def test_v161_no_inline_handlers():
    page=(ROOT/'templates'/'money_hunter.html').read_text(encoding='utf-8').lower()
    for token in (' onclick=', ' onsubmit=', ' onchange=', ' onload='):
        assert token not in page

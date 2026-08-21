from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def test_actions_route_separates_active_and_history():
    t=(ROOT/'profitos'/'routes'/'actions.py').read_text(encoding='utf-8')
    assert "WHERE status IN ('PENDING','APPROVED')" in t
    assert "WHERE status NOT IN ('PENDING','APPROVED')" in t
    assert "history_rows=history_rows" in t


def test_cancelled_action_can_be_reactivated():
    t=(ROOT/'templates'/'actions.html').read_text(encoding='utf-8')
    assert "a.status=='CANCELLED'" in t
    assert 'value="PENDING"' in t
    assert 'Réactiver' in t


def test_cancelled_actions_move_to_history():
    t=(ROOT/'templates'/'actions.html').read_text(encoding='utf-8')
    assert 'Actions actives' in t
    assert 'Historique ({{ history_rows|length }})' in t


def test_actions_text_is_utf8_clean():
    t=(ROOT/'templates'/'actions.html').read_text(encoding='utf-8')
    assert 'Décider avant d\'agir.' in t
    assert 'exécutée automatiquement —' in t
    assert 'ÔÇ' not in t
    assert '├' not in t


def test_action_source_drafts_are_utf8_clean():
    t=(ROOT/'profitos'/'routes'/'actions.py').read_text(encoding='utf-8')
    assert 'présente un solde' in t
    assert 'arrivé à échéance' in t
    assert '€' in t
    assert 'ÔÇ' not in t
    assert '├' not in t

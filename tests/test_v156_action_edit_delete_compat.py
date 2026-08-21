from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]


def test_no_inline_event_handler_in_actions_template():
    t=(ROOT/'templates'/'actions.html').read_text(encoding='utf-8')
    assert not re.search(r"\son[a-z]+\s*=", t, re.I)


def test_v154_render_contract_preserved():
    t=(ROOT/'profitos'/'routes'/'actions.py').read_text(encoding='utf-8')
    assert "history_rows=history_rows" in t


def test_no_literal_mojibake_in_actions_source():
    t=(ROOT/'profitos'/'routes'/'actions.py').read_text(encoding='utf-8')
    assert 'ÔÇ' not in t
    assert '├' not in t


def test_legacy_cleanup_is_generic():
    t=(ROOT/'profitos'/'routes'/'actions.py').read_text(encoding='utf-8')
    assert "text.encode('cp850').decode('utf-8')" in t


def test_delete_and_edit_features_remain():
    py=(ROOT/'profitos'/'routes'/'actions.py').read_text(encoding='utf-8')
    html=(ROOT/'templates'/'actions.html').read_text(encoding='utf-8')
    assert "def action_edit(aid):" in py
    assert "def action_delete(aid):" in py
    assert "if a['status']!='CANCELLED':" in py
    assert "Modifier le texte" in html
    assert "Réactiver" in html
    assert "Supprimer" in html

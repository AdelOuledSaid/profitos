from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def test_action_edit_route_exists():
    t=(ROOT/'profitos'/'routes'/'actions.py').read_text(encoding='utf-8')
    assert "@app.route('/actions/<int:aid>/edit',methods=['POST'])" in t
    assert "def action_edit(aid):" in t
    assert "UPDATE actions SET title=?,draft=? WHERE id=?" in t


def test_only_cancelled_actions_can_be_deleted():
    t=(ROOT/'profitos'/'routes'/'actions.py').read_text(encoding='utf-8')
    assert "@app.route('/actions/<int:aid>/delete',methods=['POST'])" in t
    assert "if a['status']!='CANCELLED':" in t
    assert "DELETE FROM actions WHERE id=?" in t


def test_ui_has_edit_and_delete_controls():
    t=(ROOT/'templates'/'actions.html').read_text(encoding='utf-8')
    assert "Modifier le texte" in t
    assert "Enregistrer les modifications" in t
    assert "url_for('action_delete',aid=a.id)" in t
    assert "Supprimer définitivement cette action annulée ?" in t


def test_cancelled_action_still_can_be_reactivated():
    t=(ROOT/'templates'/'actions.html').read_text(encoding='utf-8')
    assert 'value="PENDING"' in t
    assert 'Réactiver' in t


def test_legacy_text_cleanup_present():
    t=(ROOT/'profitos'/'routes'/'actions.py').read_text(encoding='utf-8')
    assert 'def clean_legacy_text(value):' in t
    assert "text.encode('cp850').decode('utf-8')" in t


def test_new_drafts_are_clean_utf8():
    t=(ROOT/'profitos'/'routes'/'actions.py').read_text(encoding='utf-8')
    assert 'présente un solde' in t
    assert 'arrivé à échéance' in t
    assert '€' in t
    assert 'ÔÇ' not in t
    assert '├' not in t

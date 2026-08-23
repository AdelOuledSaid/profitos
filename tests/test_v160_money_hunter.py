from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def test_money_hunter_module_registered():
    t=(ROOT/'profitos'/'__init__.py').read_text(encoding='utf-8')
    assert 'money_hunter.register(app)' in t


def test_money_hunter_route_is_paid_and_active():
    t=(ROOT/'profitos'/'routes'/'money_hunter.py').read_text(encoding='utf-8')
    assert "@app.route('/money-hunter')" in t
    assert '@requires_active_plan' in t
    assert '@requires_paid_plan' in t


def test_money_hunter_uses_existing_financial_data_only():
    t=(ROOT/'profitos'/'routes'/'money_hunter.py').read_text(encoding='utf-8')
    assert "FROM invoices" in t
    assert "type='SAVE'" in t
    assert "type='GROW'" in t
    assert 'money_identified' in t
    assert 'recover_expected' in t
    assert 'save_expected' in t


def test_money_hunter_does_not_invent_grow_value():
    t=(ROOT/'profitos'/'routes'/'money_hunter.py').read_text(encoding='utf-8')
    assert "if amount > 0:" in t
    assert "'expected': 0.0" in t


def test_money_hunter_ui_exists_and_is_linked():
    base=(ROOT/'templates'/'base.html').read_text(encoding='utf-8')
    page=(ROOT/'templates'/'money_hunter.html').read_text(encoding='utf-8')
    assert "url_for('money_hunter')" in base
    assert 'Money Hunter' in base
    assert "Où est l'argent aujourd'hui ?" in page
    assert 'Les prochaines décisions recommandées' in page


def test_no_inline_event_handlers_money_hunter():
    page=(ROOT/'templates'/'money_hunter.html').read_text(encoding='utf-8').lower()
    for token in (' onclick=', ' onsubmit=', ' onchange=', ' onload='):
        assert token not in page

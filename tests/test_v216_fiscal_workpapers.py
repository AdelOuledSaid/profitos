from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def text(p): return (ROOT/p).read_text(encoding='utf-8')

def test_schema_and_entity_scope():
    s=text('profitos/runtime.py')
    assert 'CREATE TABLE IF NOT EXISTS fiscal_workpapers' in s
    assert 'entity_id INTEGER' in s
    assert 'trial_balance_json TEXT NOT NULL' in s
    assert 'checks_json TEXT NOT NULL' in s
    assert 'snapshot_max_entry_id INTEGER' in s

def test_workpaper_types_and_no_fake_filing():
    s=text('profitos/routes/accounting.py')
    assert "'LIASSE','IS','CVAE','DAS2'" in s
    assert "Aucun formulaire EDI-TDFC" in s
    assert "def fiscal_workpapers():" in s
    assert "'validated' if action == 'validate' else 'draft'" in s

def test_validation_requires_checks_and_closure():
    s=text('profitos/routes/accounting.py')
    assert "not all(x['ok'] for x in checks)" in s
    assert "Comptabilité clôturée jusqu’à la fin de période" in s
    assert "Balance générale débit = crédit" in s

def test_ui_disclaimer_and_navigation():
    t=text('templates/fiscal_workpapers.html')
    b=text('templates/base.html')
    assert "Aucun EDI-TDFC ni dépôt DGFiP" in t
    assert "ne détermine pas ici l'éligibilité fiscale" in t
    assert "url_for('fiscal_workpapers')" in b

from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
RUNTIME=(ROOT/'profitos/runtime.py').read_text(encoding='utf-8')
ACC=(ROOT/'profitos/routes/accounting.py').read_text(encoding='utf-8')
TPL=(ROOT/'templates/vat_summary.html').read_text(encoding='utf-8')

def test_vat_declaration_registry_is_entity_scoped_and_unique_per_period():
    assert 'CREATE TABLE IF NOT EXISTS vat_declarations' in RUNTIME
    assert 'entity_id INTEGER' in RUNTIME
    assert 'UNIQUE(entity_id,period_start,period_end)' in RUNTIME
    assert 'snapshot_max_entry_id' in RUNTIME

def test_vat_snapshot_uses_real_accounting_vat_accounts_and_entity():
    assert "l.account_code='445710'" in ACC
    assert "l.account_code='445660'" in ACC
    assert "e.entity_id=?" in ACC
    assert 'credit-l.debit' in ACC and 'debit-l.credit' in ACC

def test_vat_workflow_has_prepare_lock_file_and_reopen_without_fake_telefiling():
    for action in ("'prepare'","'lock'","'file'","'reopen'"):
        assert action in ACC
    assert "ProfitOS prépare et trace le dossier mais ne télédéclare pas" in ACC
    assert 'filing_reference' in ACC

def test_vat_lock_requires_consistency_checks_and_detects_stale_snapshot():
    assert '_vat_consistency_checks' in ACC
    assert "if action in ('lock','file') and not all(x['ok'] for x in checks)" in ACC
    assert "max_entry_id > int(declaration['snapshot_max_entry_id'] or 0)" in ACC
    assert 'Écritures comptables équilibrées' in ACC
    assert 'Factures clients émises comptabilisées' in ACC
    assert 'Factures fournisseurs validées comptabilisées' in ACC

def test_vat_ui_exposes_controls_status_and_staleness_warning():
    assert 'TVA & dossier déclaratif' in TPL
    assert 'Contrôles de cohérence' in TPL
    assert 'Verrouiller pour contrôle' in TPL
    assert 'Marquer déclarée' in TPL
    assert 'À recontrôler' in TPL

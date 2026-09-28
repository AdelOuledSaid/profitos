from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def test_tax_preparation_schema_and_types():
    runtime=(ROOT/'profitos/runtime.py').read_text(encoding='utf-8')
    assert 'tax_declaration_preparations' in runtime
    assert 'declaration_type TEXT NOT NULL' in runtime
    assert 'checks_json TEXT' in runtime
    assert 'validated_at TEXT' in runtime

def test_ca3_ca12_internal_validation_only():
    route=(ROOT/'profitos/routes/accounting.py').read_text(encoding='utf-8')
    tpl=(ROOT/'templates/vat_summary.html').read_text(encoding='utf-8')
    assert "('CA3','CA12')" in route
    assert "action in ('fiscal_prepare','fiscal_validate')" in route
    assert 'Aucune télédéclaration DGFiP' in route
    assert 'ne constitue ni un dépôt, ni une télédéclaration' in tpl

def test_validation_requires_consistency_checks_and_snapshot():
    route=(ROOT/'profitos/routes/accounting.py').read_text(encoding='utf-8')
    assert "action=='fiscal_validate' and not all(x['ok'] for x in checks)" in route
    assert 'snapshot_max_entry_id' in route
    assert 'checks_json=json.dumps(checks' in route

def test_fiscal_preparation_is_entity_scoped():
    route=(ROOT/'profitos/routes/accounting.py').read_text(encoding='utf-8')
    assert "SELECT * FROM tax_declaration_preparations WHERE declaration_type=?" in route
    assert "'entity_id=?' if eid else 'entity_id IS NULL'" in route

from pathlib import Path

CORE=Path("profitos/runtime.py").read_text(encoding="utf-8")
RUNTIME=Path("profitos/runtime.py").read_text(encoding="utf-8")
IMPORTS=Path("profitos/routes/imports.py").read_text(encoding="utf-8")
MAIN=Path("profitos/routes/main.py").read_text(encoding="utf-8")

def test_entity_scoped_dso_schema_exists_in_both_runtime_schemas():
    for source in (CORE,RUNTIME):
        assert "CREATE TABLE IF NOT EXISTS dso_entity_snapshots" in source
        assert "UNIQUE(snapshot_date, entity_key)" in source

def test_import_writes_dso_for_root_and_subentities():
    assert "if not entity_id:" in IMPORTS  # legacy-root compatibility only
    assert "INSERT INTO dso_entity_snapshots" in IMPORTS
    assert "entity_key=str(entity_id) if entity_id else '__ROOT__'" in IMPORTS

def test_dashboard_reads_current_entity_dso_only():
    assert "FROM dso_entity_snapshots WHERE entity_key=?" in MAIN
    assert "entity_key=str(eid) if eid else '__ROOT__'" in MAIN

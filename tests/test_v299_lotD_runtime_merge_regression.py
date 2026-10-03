from pathlib import Path
R=Path("profitos/runtime.py").read_text(encoding="utf-8")
C=Path("profitos/core/runtime.py").read_text(encoding="utf-8")

def test_v299_preserves_lotA_dso_schema_in_both_runtimes():
    for source in (R,C):
        assert "CREATE TABLE IF NOT EXISTS dso_entity_snapshots" in source

def test_v299_preserves_v298_idempotency_hardening():
    b=R.split("def api_require_idempotency():",1)[1].split("\ndef ",1)[0]
    assert "len(key)>128" in b
    assert "ord(ch) < 32" in b
    assert "ord(ch) == 127" in b

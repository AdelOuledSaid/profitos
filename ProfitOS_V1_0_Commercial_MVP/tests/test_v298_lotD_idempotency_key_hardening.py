from pathlib import Path
T=Path("profitos/runtime.py").read_text(encoding="utf-8")
def test_idempotency_key_rejects_control_characters():
    b=T.split("def api_require_idempotency():",1)[1].split("\ndef ",1)[0]
    assert "len(key)>128" in b
    assert "ord(ch) < 32" in b
    assert "ord(ch) == 127" in b
    assert "caractères imprimables" in b

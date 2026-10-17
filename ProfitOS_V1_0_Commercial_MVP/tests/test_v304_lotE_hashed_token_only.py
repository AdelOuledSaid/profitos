from pathlib import Path
T=Path("profitos/runtime.py").read_text(encoding="utf-8")

def test_token_lookup_accepts_only_digest_storage():
    block=T.split("def _token_user(kind, raw_token):",1)[1].split("def send_verification_email",1)[0]
    assert "digest=token_digest(raw_token)" in block
    assert "WHERE {column}=?',(digest,)" in block
    assert "WHERE {column}=?',(raw_token,)" not in block
    assert "stored=raw_token" not in block

def test_token_lookup_returns_digest_for_atomic_consumption():
    block=T.split("def _token_user(kind, raw_token):",1)[1].split("def send_verification_email",1)[0]
    assert "return u,digest" in block

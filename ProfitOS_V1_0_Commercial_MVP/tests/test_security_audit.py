from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def test_security_event_schema_and_logger_exist():
    text=(ROOT/"profitos"/"runtime.py").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS security_events" in text
    assert "def log_security_event(" in text
    assert "ip_hash" in text
    # L'IP vient de request.remote_addr corrigé par ProxyFix : un en-tête client
    # (CF-Connecting-IP, X-Forwarded-For brut) ne doit plus pouvoir l'imposer.
    body=text.split("def _audit_ip_hash",1)[1].split("\ndef ",1)[0]
    assert "request.remote_addr" in body and "CF-Connecting-IP" not in body

def test_security_log_does_not_store_raw_ip_column():
    text=(ROOT/"profitos"/"runtime.py").read_text(encoding="utf-8")
    schema=text.split("CREATE TABLE IF NOT EXISTS security_events",1)[1].split(");",1)[0]
    assert "ip_hash TEXT" in schema
    assert "\nip TEXT" not in schema

def test_auth_events_are_instrumented():
    text=(ROOT/"profitos"/"routes"/"account.py").read_text(encoding="utf-8")
    for event in ("LOGIN_FAILED","LOGIN_SUCCESS","PASSWORD_RESET_REQUEST","PASSWORD_RESET","EMAIL_VERIFY","TEAM_ROLE_UPDATE","TEAM_REMOVE"):
        assert event in text

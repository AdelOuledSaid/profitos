from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def test_security_event_schema_and_logger_exist():
    text=(ROOT/"profitos"/"runtime.py").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS security_events" in text
    assert "def log_security_event(" in text
    assert "ip_hash" in text
    assert "CF-Connecting-IP" in text

def test_security_log_does_not_store_raw_ip_column():
    text=(ROOT/"profitos"/"runtime.py").read_text(encoding="utf-8")
    schema=text.split("CREATE TABLE IF NOT EXISTS security_events",1)[1].split(");",1)[0]
    assert "ip_hash TEXT" in schema
    assert "\nip TEXT" not in schema

def test_auth_events_are_instrumented():
    text=(ROOT/"profitos"/"routes"/"account.py").read_text(encoding="utf-8")
    for event in ("LOGIN_FAILED","LOGIN_SUCCESS","PASSWORD_RESET_REQUEST","PASSWORD_RESET","EMAIL_VERIFY","TEAM_ROLE_UPDATE","TEAM_REMOVE"):
        assert event in text

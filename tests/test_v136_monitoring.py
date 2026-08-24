from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def test_version_136():
    text=(ROOT/"profitos"/"config.py").read_text(encoding="utf-8")
    assert 'APP_VERSION = "1.6.6"' in text

def test_ops_health_route_exists():
    text=(ROOT/"profitos"/"__init__.py").read_text(encoding="utf-8")
    assert "@app.get('/ops/health')" in text
    assert "X-Response-Time-Ms" in text
    assert "HTTP_5XX" in text

def test_backup_scripts_exist():
    assert (ROOT/"scripts"/"backup_postgres.py").exists()
    assert (ROOT/"scripts"/"check_backup.py").exists()

def test_backups_gitignored():
    text=(ROOT/".gitignore").read_text(encoding="utf-8")
    assert "backups/" in text
    assert "*.dump" in text

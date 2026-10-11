"""Migrations tenant versionnées + preflight preDeploy."""
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import profitos.runtime as rt  # noqa: E402
import cloud_preflight  # noqa: E402


@pytest.fixture
def tenants(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, 'TENANTS', tmp_path)
    monkeypatch.setattr(rt, '_TENANT_SCHEMA_VERSION', None)
    return tmp_path


def test_first_migration_records_version_then_fast_path(tenants, monkeypatch):
    r1 = rt.migrate_tenant(9001)
    assert r1['status'] == 'migrated'
    calls = []
    real = rt._migrate_tenant_schema
    monkeypatch.setattr(rt, '_migrate_tenant_schema', lambda org_id=None: calls.append(org_id) or real(org_id))
    monkeypatch.setattr(rt, '_TENANT_SCHEMA_VERSION', rt.tenant_schema_version())  # version figée
    r2 = rt.migrate_tenant(9001)
    assert r2['status'] == 'up_to_date'
    assert calls == []
    assert rt.init_tenant_db(9001)['status'] == 'up_to_date'
    assert calls == []
    r3 = rt.migrate_tenant(9001, force=True)
    assert r3['status'] == 'migrated' and calls == [9001]


def test_version_change_triggers_migration(tenants, monkeypatch):
    rt.migrate_tenant(9002)
    monkeypatch.setattr(rt, '_TENANT_SCHEMA_VERSION', 'autre-version')
    assert rt.migrate_tenant(9002)['status'] == 'migrated'
    assert rt._tenant_schema_current(9002)


def test_warnings_are_collected_and_version_not_recorded(tenants, monkeypatch):
    monkeypatch.setattr(rt, '_TENANT_SCHEMA_VERSION', 'v-test')

    def fake(org_id=None):
        rt._migration_warn('[ProfitOS] ATTENTION : migration colonne x.y ignorée (boom)')
    monkeypatch.setattr(rt, '_migrate_tenant_schema', fake)
    r = rt.migrate_tenant(9003)
    assert r['status'] == 'migrated_with_warnings'
    assert 'boom' in r['warnings'][0]
    assert not rt._tenant_schema_current(9003)


def test_migrate_all_isolates_failing_tenant(tenants, monkeypatch):
    monkeypatch.setattr(rt, '_TENANT_SCHEMA_VERSION', 'v-iso')
    monkeypatch.setattr(rt, 'list_organization_ids', lambda: [1, 2, 3])
    done = []

    def fake(org_id=None):
        if org_id == 2:
            raise RuntimeError('schéma cassé')
        done.append(org_id)
    monkeypatch.setattr(rt, '_migrate_tenant_schema', fake)
    report = rt.migrate_all_tenant_schemas()
    assert done == [1, 3]
    assert sorted(report['migrated']) == [1, 3]
    assert list(report['failed']) == [2]
    with pytest.raises(RuntimeError):
        rt.initialize_all_tenant_schemas()
    # second passage : 1 et 3 sont à jour, 2 retenté
    report2 = rt.migrate_all_tenant_schemas()
    assert sorted(report2['up_to_date']) == [1, 3] and list(report2['failed']) == [2]


def test_preflight_exits_non_zero_when_a_tenant_fails(tenants, monkeypatch, capsys):
    monkeypatch.setenv('PROFITOS_ENV', 'development')
    monkeypatch.setattr(rt, 'migrate_all_tenant_schemas', lambda force=False: {
        'version': 'v', 'up_to_date': [1], 'migrated': [3], 'warnings': {3: ['ATTENTION x']}, 'failed': {2: 'RuntimeError: x'}})
    assert cloud_preflight.main(['--init-tenants']) == 1
    out = capsys.readouterr().out
    assert 'ÉCHEC migration org_2' in out and 'AVERTISSEMENT migration org_3' in out


def test_preflight_ok_without_failures(tenants, monkeypatch):
    monkeypatch.setenv('PROFITOS_ENV', 'development')
    monkeypatch.setattr(rt, 'migrate_all_tenant_schemas', lambda force=False: {
        'version': 'v', 'up_to_date': [1], 'migrated': [], 'warnings': {}, 'failed': {}})
    assert cloud_preflight.main(['--init-tenants']) == 0


def test_ensure_tenant_schema_uses_lock_and_set():
    src = (ROOT / 'profitos' / 'runtime.py').read_text(encoding='utf-8')
    assert '_tenant_schema_lock=_threading.Lock()' in src
    assert 'with _tenant_schema_lock:\n                _tenant_schema_checked.add(org_id)' in src
    assert 'print(f"[ProfitOS] ATTENTION' not in src


# --- Preflight production : stockage et email --------------------------------

def test_upload_dir_required_and_not_tmp():
    assert cloud_preflight.check_upload_dir({})
    assert any('/tmp' in e for e in cloud_preflight.check_upload_dir({'PROFITOS_UPLOAD_DIR': '/tmp/uploads'}))
    assert cloud_preflight.check_upload_dir({'PROFITOS_UPLOAD_DIR': 'relative/uploads'})


def test_upload_dir_writable_ok_and_not_writable():
    target = ROOT / 'tenant_data' / '_preflight_probe_v401'
    try:
        assert cloud_preflight.check_upload_dir({'PROFITOS_UPLOAD_DIR': str(target)}) == []
        blocker = ROOT / 'tenant_data' / '_preflight_file_v401'
        blocker.write_text('x')
        errs = cloud_preflight.check_upload_dir({'PROFITOS_UPLOAD_DIR': str(blocker / 'sub')})
        assert errs and 'inscriptible' in errs[0]
    finally:
        shutil.rmtree(target, ignore_errors=True)
        (ROOT / 'tenant_data' / '_preflight_file_v401').unlink(missing_ok=True)


def test_email_required_in_production():
    assert cloud_preflight.check_email({})
    assert cloud_preflight.check_email({'RESEND_API_KEY': 're_x'}) == []
    assert cloud_preflight.check_email({'SMTP_HOST': 'smtp.example'}) == []


def test_preflight_production_fails_without_disk_or_email(monkeypatch, capsys):
    monkeypatch.setenv('PROFITOS_ENV', 'production')
    monkeypatch.setenv('PROFITOS_SECRET_KEY', 'x' * 40)
    monkeypatch.setenv('APP_BASE_URL', 'https://app.profitos.fr')
    monkeypatch.delenv('PROFITOS_UPLOAD_DIR', raising=False)
    monkeypatch.delenv('RESEND_API_KEY', raising=False)
    monkeypatch.delenv('SMTP_HOST', raising=False)
    assert cloud_preflight.main([]) == 1
    out = capsys.readouterr().out
    assert 'PROFITOS_UPLOAD_DIR' in out and 'RESEND_API_KEY' in out

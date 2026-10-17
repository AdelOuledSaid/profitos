"""Blueprint Render, CI et fichiers d'exploitation."""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
BP = yaml.safe_load((ROOT / 'render.yaml').read_text(encoding='utf-8'))


def _service(name):
    return next(s for s in BP['services'] if s['name'] == name)


def _keys(envvars):
    return {e.get('key') or ('group:' + e['fromGroup']) for e in envvars}


def test_web_has_paid_plan_disk_and_predeploy():
    web = _service('profitos')
    assert web['plan'] == 'starter'
    assert web['disk'] == {'name': 'profitos-data', 'mountPath': '/var/data', 'sizeGB': 1}  # pré-lancement : un disque peut grandir, jamais rétrécir
    assert web['preDeployCommand'] == 'python scripts/cloud_preflight.py --init-tenants'
    assert 'cloud_preflight' not in web['startCommand']
    for flag in ('--workers 1', '--threads 4', '--timeout 120', '--graceful-timeout 30', '--access-logfile -'):
        assert flag in web['startCommand']
    env = {e.get('key'): e for e in web['envVars'] if e.get('key')}
    assert env['PROFITOS_UPLOAD_DIR']['value'] == '/var/data/uploads'
    assert not env['PROFITOS_UPLOAD_DIR']['value'].startswith('/tmp')
    assert 'group:profitos-shared' in _keys(web['envVars'])
    assert web['autoDeployTrigger'] in ('checksPass', 'commit')


def test_no_free_plans_and_plausible_db_plan():
    # Web et cron jamais gratuits (mise en veille, pas de disque) ; seul le Key Value
    # (compteurs de limitation de débit) peut l'être en pré-lancement.
    assert all(s.get('plan') != 'free' for s in BP['services'] if s['type'] != 'keyvalue')
    assert BP['databases'][0]['plan'].startswith(('basic-', 'pro-', 'accelerated-'))


def test_cron_runs_dispatcher_with_shared_env():
    cron = _service('profitos-jobs')
    assert cron['type'] == 'cron'
    assert cron['startCommand'] == 'python scripts/run_job.py --due'
    assert cron['schedule'].split()[1:] == ['*', '*', '*', '*']  # horaire
    keys = _keys(cron['envVars'])
    assert {'group:profitos-shared', 'DATABASE_URL'} <= keys
    group = next(g for g in BP['envVarGroups'] if g['name'] == 'profitos-shared')
    gkeys = {e['key'] for e in group['envVars']}
    assert {'PROFITOS_SECRET_KEY', 'JOBS_TRIGGER_TOKEN', 'APP_BASE_URL', 'PROFITOS_ENV'} <= gkeys
    # la clé secrète ne doit pas être générée deux fois (web et groupe)
    assert 'PROFITOS_SECRET_KEY' not in _keys(_service('profitos')['envVars'])


def test_ci_workflow_runs_pytest_on_push_and_pr():
    wf = yaml.safe_load((ROOT / '.github' / 'workflows' / 'tests.yml').read_text(encoding='utf-8'))
    triggers = wf.get('on') or wf.get(True)
    assert 'push' in triggers and 'pull_request' in triggers
    steps = ' '.join(str(s.get('run', '')) + str(s.get('with', '')) for s in wf['jobs']['tests']['steps'])
    assert 'pytest' in steps and 'requirements.txt' in steps
    assert '3.11' in wf['jobs']['tests']['strategy']['matrix']['python-version']


def test_procfile_and_railway_run_migrations_before_start():
    proc = (ROOT / 'Procfile').read_text(encoding='utf-8')
    assert proc.startswith('release: python scripts/cloud_preflight.py --init-tenants')
    assert '--graceful-timeout 30' in proc


def test_repo_hygiene():
    for stray in ('assert', 'git', 'pytest', 'README_HOTFIX.txt', 'db.py'):
        assert not (ROOT / stray).exists(), stray
    assert not (ROOT / 'profitos' / 'core').exists()
    gi = (ROOT / '.gitignore').read_text(encoding='utf-8')
    for pat in ('backups/', '*.db', 'uploads/', 'tenant_data/', 'logs/'):
        assert pat in gi
    assert (ROOT / 'TEST-PROFITOS-003.xml').exists()

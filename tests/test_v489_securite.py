"""v489 — Dossier sécurité : contrôle en lecture seule avant clients réels, liens secrets des
missions d'experts jamais mis en cache, documentation des contrôles."""
import importlib.util
from pathlib import Path

import profitos.runtime as rt
from test_v470_encaissement import app, signup  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('profitos_security_check', ROOT / 'scripts' / 'security_check.py')
SC = importlib.util.module_from_spec(spec)
spec.loader.exec_module(SC)

GOOD = {'PROFITOS_SECRET_KEY': 's' * 40, 'PROFITOS_DATA_KEY': 'd' * 40, 'PROFITOS_ENV': 'production',
        'APP_BASE_URL': 'https://app.exemple.fr', 'DATABASE_URL': 'postgresql://u@h/db', 'REDIS_URL': 'redis://h:6379'}


def levels(items):
    return {i['label']: i['level'] for i in items}


def test_lien_mission_expert_jamais_mis_en_cache(app):
    r = app.test_client().get('/expert/mission/jeton-inexistant')
    assert 'no-store' in r.headers.get('Cache-Control', '')


def test_secrets_conformes_et_cles_identiques_refusees():
    assert all(i['level'] == SC.OK for i in SC.check_secrets(GOOD))
    bad = dict(GOOD, PROFITOS_DATA_KEY=GOOD['PROFITOS_SECRET_KEY'], APP_BASE_URL='http://app', PROFITOS_ENV='')
    items = SC.check_secrets(bad)
    fails = [i['label'] for i in items if i['level'] == SC.FAIL]
    assert any('identique' in f for f in fails)
    assert any('HTTPS' in f for f in fails) and any('production' in f for f in fails)
    assert not any(GOOD['PROFITOS_SECRET_KEY'] in (i['label'] + i['detail']) for i in items)  # aucun secret affiché


def test_redis_absent_avertit():
    assert SC.check_rate_limit({})[0]['level'] == SC.WARN
    assert SC.check_rate_limit(GOOD)[0]['level'] == SC.OK


def test_liens_secrets_et_fichiers_internes(app):
    items = SC.check_http(app)
    token = [i for i in items if i['label'].startswith('Lien secret')]
    assert len(token) == 3 and all(i['level'] == SC.OK for i in token)
    internal = [i for i in items if i['label'].startswith('Fichier interne')]
    assert internal and all(i['level'] == SC.OK for i in internal)


def test_double_authentification_et_admin_plateforme(app):
    cl, org = signup(app)
    ac = rt.auth_cx()
    email = ac.execute("SELECT u.email FROM users u JOIN memberships m ON m.user_id=u.id WHERE m.organization_id=?",
                       (org,)).fetchone()['email']
    ac.execute('UPDATE users SET totp_enabled=0, email_verified=1 WHERE email=?', (email,))
    ac.commit()
    items = SC.check_accounts(ac, {'PLATFORM_ADMIN_EMAILS': email})
    by = levels(items)
    assert any(k.startswith('Double authentification') and v == SC.WARN for k, v in by.items())
    assert by['Compte d\'administration plateforme vérifié et protégé par 2FA'] == SC.FAIL
    assert not any(email in (i['label'] + i['detail']) for i in items)  # aucune adresse affichée
    ac.execute('UPDATE users SET totp_enabled=1 WHERE email=?', (email,))
    ac.commit()
    assert levels(SC.check_accounts(ac, {'PLATFORM_ADMIN_EMAILS': email}))[
        'Compte d\'administration plateforme vérifié et protégé par 2FA'] == SC.OK
    ac.close()


def test_execution_complete_sans_erreur(app):
    signup(app)
    ac = rt.auth_cx()
    items = SC.run(env=GOOD, app=app, ac=ac)
    ac.close()
    sections = {i['section'] for i in items}
    assert {'Secrets et chiffrement', 'Protection HTTP', 'Comptes et accès', 'Traçabilité', 'Sauvegardes'} <= sections
    assert all(i['level'] in (SC.OK, SC.WARN, SC.FAIL, SC.INFO) for i in items)


def test_documentation_securite():
    doc = (ROOT / 'docs' / 'securite.md').read_text(encoding='utf-8')
    for needle in ('security_check.py', 'Restauration', 'Incident', 'Sous-traitants', 'test_v'):
        assert needle in doc


def test_requete_sans_jeton_csrf_refusee(app):
    cl, org = signup(app)
    with cl.session_transaction() as s:
        s['csrf_token'] = 'jeton-attendu'
    r = cl.post('/mission-control/suivi', data={'action': 'fait'})
    assert r.status_code == 400
    r = cl.post('/mission-control/suivi', data={'action': 'fait', 'csrf_token': 'autre-jeton'})
    assert r.status_code == 400

"""v419 — RGPD : export complet (ZIP de CSV), export personnel, suppression
programmée avec délai de grâce, purges planifiées, anonymisation."""
import io
import zipfile
from datetime import datetime, timedelta, timezone

from support_v4xx import PASSWORD, app, auth, flashes, login, post, signup, tenant  # noqa: F401


def test_organization_export_contains_every_tenant_table(app):
    client, _, org_id, _ = signup(app, 'Export SAS', plan='BUSINESS')
    tc = tenant(org_id)
    tables = {r['name'] for r in tc.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}
    tc.execute("INSERT INTO ecommerce_connections(platform,shop_domain,credential_1,credential_2,connected_at,connected_by) "
               "VALUES('shopify','s.myshopify.com','shpat_SECRET','',?,?)", ('2026-01-01', 'a'))
    tc.commit(); tc.close()
    r = client.get('/settings/gdpr-export')
    assert r.status_code == 200 and r.mimetype == 'application/zip'
    z = zipfile.ZipFile(io.BytesIO(r.data))
    names = set(z.namelist())
    assert len(tables) > 50
    assert {f'organisation/{t}.csv' for t in tables} <= names
    assert 'compte/membres.csv' in names
    eco = z.read('organisation/ecommerce_connections.csv').decode('utf-8-sig')
    assert ';' in eco.splitlines()[0] and 'shpat_SECRET' not in eco and '[masqué]' in eco


def test_every_user_can_export_personal_data(app):
    owner, _, org_id, _ = signup(app, 'Perso SAS', plan='BUSINESS')
    member, email, _, mid = signup(app, 'Autre Org')
    auth("INSERT INTO memberships(user_id,organization_id,role,created_at) VALUES(?,?,'COMMERCIAL','2026-01-01')", (mid, org_id))
    with member.session_transaction() as s:
        s['org_id'] = org_id
    assert member.get('/settings/gdpr-export').status_code == 302  # export de l'organisation : propriétaire/admin
    r = member.get('/compte/mes-donnees')
    z = zipfile.ZipFile(io.BytesIO(r.data))
    assert email in z.read('profil.csv').decode('utf-8-sig')
    assert 'password_hash' not in z.read('profil.json').decode()
    assert 'Perso SAS' in z.read('adhesions.csv').decode('utf-8-sig')


def test_deletion_is_scheduled_then_purged_after_grace_period(app):
    from profitos import retention
    client, email, org_id, uid = signup(app, 'Supprimée SAS')
    page = client.get('/settings/delete-account').get_data(as_text=True)
    assert '10 ans' in page and 'FEC' in page
    post(client, '/settings/delete-account', {'confirm_text': 'Supprimée SAS', 'current_password': PASSWORD})
    org = auth('SELECT deleted_at,purge_after FROM organizations WHERE id=?', (org_id,), 'one')
    assert org['deleted_at'] and org['purge_after']
    # Organisation inaccessible pendant le délai de grâce, restaurable par le propriétaire.
    c2, _ = login(app, email)
    assert '/organisation/suppression-programmee' in c2.get('/facturation').headers['Location']
    post(c2, '/organisation/suppression-programmee/annuler', {'current_password': PASSWORD})
    assert auth('SELECT deleted_at FROM organizations WHERE id=?', (org_id,), 'one')['deleted_at'] is None

    post(c2, '/settings/delete-account', {'confirm_text': 'Supprimée SAS', 'current_password': PASSWORD})
    assert retention.purge_deleted_organizations() == 0  # délai non écoulé
    with app.app_context():
        purged = retention.purge_deleted_organizations(now=datetime.now(timezone.utc) + timedelta(days=15))
    assert purged == 1
    assert auth('SELECT 1 FROM organizations WHERE id=?', (org_id,), 'one') is None
    assert auth('SELECT 1 FROM users WHERE id=?', (uid,), 'one') is None


def test_log_retention_and_demo_purge(app):
    from profitos import retention
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    recent = datetime.now(timezone.utc).isoformat()
    auth("INSERT INTO activity_log(organization_id,user_id,event_type,description,created_at) VALUES(NULL,NULL,'T','old',?)", (old,))
    auth("INSERT INTO activity_log(organization_id,user_id,event_type,description,created_at) VALUES(NULL,NULL,'T','new',?)", (recent,))
    auth("INSERT INTO security_events(event_type,outcome,created_at) VALUES('T','X',?)", (old,))
    auth("INSERT INTO demo_requests(full_name,email,created_at) VALUES('Vieux','v@example.com',?)",
         ((datetime.now(timezone.utc) - timedelta(days=4 * 365)).isoformat(),))
    with app.app_context():
        counts = retention.purge_expired_logs()
        demos = retention.purge_old_demo_requests()
    assert counts['activity_log'] >= 1 and counts['security_events'] >= 1 and demos >= 1
    assert auth("SELECT 1 FROM activity_log WHERE description='new' AND created_at=?", (recent,), 'one')
    assert auth("SELECT 1 FROM activity_log WHERE description='old' AND created_at=?", (old,), 'one') is None


def test_removed_member_push_tokens_deleted_and_anonymized_after_30_days(app):
    from profitos import retention
    owner, _, org_id, _ = signup(app, 'Équipe SAS', plan='BUSINESS')
    _, email, other_org, mid = signup(app, 'Org Perso')
    auth('DELETE FROM memberships WHERE user_id=?', (mid,))
    auth("INSERT INTO memberships(user_id,organization_id,role,created_at) VALUES(?,?,'MEMBER','2026-01-01')", (mid, org_id))
    auth("INSERT INTO push_device_tokens(user_id,platform,token,created_at) VALUES(?,'android','tok',?)", (mid, '2026-01-01'))
    post(owner, f'/team/{mid}/remove', {'current_password': PASSWORD})
    assert auth('SELECT COUNT(*) n FROM push_device_tokens WHERE user_id=?', (mid,), 'one')['n'] == 0
    assert auth('SELECT membership_lost_at FROM users WHERE id=?', (mid,), 'one')['membership_lost_at']
    with app.app_context():
        assert retention.anonymize_orphan_users() == 0
        assert retention.anonymize_orphan_users(now=datetime.now(timezone.utc) + timedelta(days=31)) == 1
    u = auth('SELECT email,full_name,is_active,anonymized_at FROM users WHERE id=?', (mid,), 'one')
    assert u['email'] != email and u['full_name'] is None and u['is_active'] == 0 and u['anonymized_at']


def test_revoke_third_party_tokens_is_best_effort(app, monkeypatch):
    from profitos import retention, cloud_storage
    _, _, org_id, _ = signup(app, 'Révocation SAS')
    tc = tenant(org_id)
    tc.execute("INSERT INTO cloud_storage_connections(provider,access_token,refresh_token,connected_at,connected_by,status) "
               "VALUES('dropbox','tok','ref','2026-01-01','a','active')")
    tc.commit(); tc.close()
    calls = []
    monkeypatch.setattr(cloud_storage, 'revoke_tokens', lambda row, org_id=None: calls.append(row['provider']) or True)
    with app.app_context():
        assert retention.revoke_third_party_tokens(org_id)['cloud_storage'] == 1
    assert calls == ['dropbox']

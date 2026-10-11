"""v413 — Enregistrer une restriction d'entité sans aucune entité cochée ne doit
pas transformer un accès limité en accès à toutes les entités."""
from support_v4xx import app, auth, flashes, post, signup, tenant  # noqa: F401


def test_empty_restriction_is_refused_and_previous_rows_kept(app):
    owner, _, org_id, _ = signup(app, 'Groupe Multi', plan='MULTI')
    _, _, _, member_id = signup(app, 'Ailleurs')
    auth("INSERT INTO memberships(user_id,organization_id,role,created_at) VALUES(?,?,'MEMBER','2026-01-01')", (member_id, org_id))
    tc = tenant(org_id)
    tc.execute("INSERT INTO user_entity_access(user_id,entity_id,created_at) VALUES(?,NULL,'2026-01-01')", (member_id,))
    tc.commit(); tc.close()

    r = post(owner, f'/team/{member_id}/entites', {'mode': 'restricted'})
    assert r.status_code == 302
    assert any('au moins une entité' in m for m in flashes(owner))
    tc = tenant(org_id)
    n = tc.execute('SELECT COUNT(*) n FROM user_entity_access WHERE user_id=?', (member_id,)).fetchone()['n']
    tc.close()
    assert n == 1  # toujours restreint

    # Des valeurs inconnues seules équivalent aussi à une liste vide.
    post(owner, f'/team/{member_id}/entites', {'mode': 'restricted', 'entity_id': '999'})
    tc = tenant(org_id)
    assert tc.execute('SELECT COUNT(*) n FROM user_entity_access WHERE user_id=?', (member_id,)).fetchone()['n'] == 1
    tc.close()

from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
RT=(ROOT/"profitos/runtime.py").read_text(encoding="utf-8")
RV=(ROOT/"profitos/routes/reviews.py").read_text(encoding="utf-8")
TPL=(ROOT/"templates/accountant_collaboration.html").read_text(encoding="utf-8")

def test_v208_invitation_registry_is_hashed_expiring_and_entity_scoped():
    assert "CREATE TABLE IF NOT EXISTS accountant_invitations" in RT
    assert "token_hash TEXT NOT NULL UNIQUE" in RT
    assert "entity_id INTEGER" in RT
    assert "expires_at TEXT NOT NULL" in RT
    assert "accepted_by_user_id INTEGER" in RT

def test_v208_only_owner_admin_can_create_or_revoke_invites():
    create=RV[RV.index("def accountant_invite_create"):RV.index("def accountant_invite_accept")]
    revoke=RV[RV.index("def accountant_invite_revoke"):]
    assert "current_role() not in ('OWNER','ADMIN')" in create
    assert "current_role() not in ('OWNER','ADMIN')" in revoke

def test_v208_raw_token_is_never_persisted():
    create=RV[RV.index("def accountant_invite_create"):RV.index("def accountant_invite_accept")]
    assert "hashlib.sha256(raw.encode()).hexdigest()" in create
    assert "token_hash" in create
    assert "raw invitation token is persisted" in create

def test_v208_acceptance_requires_logged_in_matching_email_and_expiry():
    accept=RV[RV.index("def accountant_invite_accept"):RV.index("def accountant_invite_revoke")]
    assert "@login_required" in RV[RV.rfind("@app.route",0,RV.index("def accountant_invite_accept")):RV.index("def accountant_invite_accept")]
    assert "user['email']" in accept and "inv['email']" in accept
    assert "inv['expires_at']" in accept
    assert "abort(403)" in accept

def test_v208_acceptance_grants_accountant_membership_and_one_entity_only():
    accept=RV[RV.index("def accountant_invite_accept"):RV.index("def accountant_invite_revoke")]
    assert "role='COMPTABLE'" in accept or "'COMPTABLE'" in accept
    assert "DELETE FROM user_entity_access WHERE user_id=?" in accept
    assert "INSERT INTO user_entity_access(user_id,entity_id)" in accept
    assert "(user['id'],inv['entity_id'])" in accept

def test_v208_revoke_removes_entity_access_and_is_audited():
    revoke=RV[RV.index("def accountant_invite_revoke"):]
    assert "DELETE FROM user_entity_access WHERE user_id=? AND entity_id IS ?" in revoke
    assert "INVITATION_REVOKED" in revoke
    assert "status='revoked'" in revoke

def test_v208_collaboration_ui_exposes_secure_invitation():
    assert "Créer une invitation sécurisée" in TPL
    assert "csrf_token()" in TPL

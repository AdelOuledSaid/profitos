from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=(ROOT/"profitos/runtime.py").read_text(encoding="utf-8")
ROUTES=(ROOT/"profitos/routes/reviews.py").read_text(encoding="utf-8")
DETAIL=(ROOT/"templates/review_detail.html").read_text(encoding="utf-8")
COLLAB=(ROOT/"templates/accountant_collaboration.html").read_text(encoding="utf-8")

def test_v203_schema_is_entity_scoped_and_auditable():
    assert "CREATE TABLE IF NOT EXISTS accountant_collaborations" in RUNTIME
    assert "CREATE TABLE IF NOT EXISTS accountant_requests" in RUNTIME
    assert "CREATE TABLE IF NOT EXISTS accountant_request_comments" in RUNTIME
    assert "CREATE TABLE IF NOT EXISTS accountant_activity" in RUNTIME
    assert RUNTIME.count("entity_id INTEGER") >= 4

def test_v203_collaboration_routes_scope_every_lookup_to_entity():
    assert "def _collab_entity_clause" in ROUTES
    assert "current_entity_id" in ROUTES
    assert "accountant_collaboration_create" in ROUTES
    assert "accountant_request_create" in ROUTES
    assert "accountant_request_comment" in ROUTES
    assert "accountant_request_status" in ROUTES
    assert "accountant_collaboration_close" in ROUTES

def test_v203_requests_have_guarded_state_transitions():
    assert "target not in ('open','resolved')" in ROUTES
    assert "Rouvrez la demande avant" in ROUTES
    assert "collaboration_status" in ROUTES
    assert "Cette collaboration est clôturée." in ROUTES

def test_v203_activity_log_covers_core_actions():
    for event in ("COLLABORATION_CREATED","REQUEST_CREATED","COMMENT_ADDED",
                  "REQUEST_RESOLVED","REQUEST_REOPENED","COLLABORATION_CLOSED"):
        assert event in ROUTES

def test_v203_ui_is_integrated_with_review():
    assert "accountant_collaboration" in DETAIL
    assert "Nouvelle demande" in COLLAB
    assert "Journal d’activité" in COLLAB
    assert "csrf_token" in COLLAB

def test_v203_collaboration_does_not_modify_accounting_entries():
    block=ROUTES[ROUTES.index("def accountant_collaboration("):]
    assert "INSERT INTO accounting_entries" not in block
    assert "UPDATE accounting_entries" not in block

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def src(rel):
    return (ROOT / rel).read_text(encoding='utf-8')

def test_supplier_inbox_is_entity_routed():
    runtime = src('profitos/runtime.py')
    inv = src('profitos/routes/invoicing.py')
    assert 'supplier_inbox_entity_tokens' in runtime
    assert 'entity_key INTEGER NOT NULL' in runtime
    assert 'get_or_create_supplier_inbox_token(org_id, entity_id)' in inv
    assert 'SELECT organization_id,entity_id FROM supplier_inbox_entity_tokens WHERE token=?' in inv
    assert 'validation_status,entity_id)' in inv
    assert "'pending', entity_id" in inv

def test_analytics_is_entity_scoped_end_to_end():
    runtime = src('profitos/runtime.py')
    analytics = src('profitos/routes/analytics.py')
    accounting = src('profitos/routes/accounting.py')
    assert "('analytical_axes','entity_id')" in runtime
    assert "('analytical_tags','entity_id')" in runtime
    assert "analytical_axes WHERE entity_id IS ?" in analytics
    assert "analytical_tags WHERE axis_id=? AND entity_id IS ?" in analytics
    assert 'e.entity_id IS ?' in analytics
    assert 'WHERE t.entity_id IS ? AND ax.entity_id IS ?' in accounting

def test_action_center_is_entity_scoped():
    runtime = src('profitos/runtime.py')
    actions = src('profitos/routes/actions.py')
    assert "('actions','entity_id')" in runtime
    assert 'idx_actions_entity_status' in runtime
    assert 'WHERE entity_id IS ? AND status' in actions
    assert 'INSERT INTO actions(opportunity_id,kind,title,draft,status,expected_value,created_at,entity_id)' in actions
    assert 'FROM invoices WHERE id=? AND entity_id IS ?' in actions
    assert 'FROM opportunities WHERE id=? AND type=? AND entity_id IS ?' in actions
    assert 'FROM actions WHERE id=? AND entity_id IS ?' in actions

def test_detail_status_and_impact_do_not_cross_entities():
    main = src('profitos/routes/main.py')
    reports = src('profitos/routes/reports.py')
    assert 'FROM actions WHERE opportunity_id=? AND kind=? AND entity_id IS ?' in main
    assert 'UPDATE invoices SET status=? WHERE id=? AND entity_id IS ?' in main
    assert 'UPDATE opportunities SET status=? WHERE id=? AND entity_id IS ?' in main
    assert "SELECT * FROM actions WHERE id=? AND entity_id IS ?" in reports
    assert "actions.entity_id IS ?" in reports

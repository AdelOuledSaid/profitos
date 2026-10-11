from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
ROUTE=(ROOT/'profitos/routes/cabinet.py').read_text(encoding='utf-8')
TPL=(ROOT/'templates/cabinet_portfolio.html').read_text(encoding='utf-8')


def test_v223_portfolio_exposes_production_indicators():
    for marker in ('reviews_pending', 'blockers', 'open_requests', 'last_entry_date',
                   'closed_until', 'entries_count', 'entities_count'):
        assert marker in ROUTE
    assert "review_diagnostic_runs" in ROUTE
    assert "accountant_requests WHERE status='open'" in ROUTE
    assert "accounting_entity_closure" in ROUTE


def test_v223_each_client_is_read_through_its_own_tenant_connection():
    block=ROUTE[ROUTE.index('def cabinet_portfolio'):ROUTE.index('def cabinet_open_client')]
    assert "tenant_cx_direct(org['id'])" in block
    assert "tc.close()" in block
    assert "JOIN organizations" not in block


def test_v223_open_client_is_membership_guarded_and_audited():
    block=ROUTE[ROUTE.index('def cabinet_open_client'):ROUTE.index("@app.route('/cabinet/temps'")]
    assert 'memberships WHERE user_id=? AND organization_id=?' in block
    assert "session['org_id'] = organization_id" in block
    assert "session['role'] = membership['role']" in block
    assert "init_tenant_db()" in block
    assert 'CABINET_CLIENT_OPENED' in block
    assert "redirect(url_for('reviews_list'))" in block


def test_v223_ui_has_cockpit_and_csrf_protected_open_action():
    for label in ('Blocages', 'Demandes', 'Dernière écriture', "Clôturé jusqu'au", 'Ouvrir →'):
        assert label in TPL
    assert 'csrf_token()' in TPL
    assert 'cabinet_open_client' in TPL

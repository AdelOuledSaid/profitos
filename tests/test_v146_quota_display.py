from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def test_billing_route_exposes_quota_usage():
    t=(ROOT/'profitos'/'routes'/'account.py').read_text(encoding='utf-8')
    assert 'from profitos.plan_usage import quota_state' in t
    assert "quota_state(" in t
    assert "'imports_per_month'" in t
    assert "'reports_per_month'" in t
    assert "quota_usage=quota_usage" in t


def test_billing_route_counts_team_and_organizations():
    t=(ROOT/'profitos'/'routes'/'account.py').read_text(encoding='utf-8')
    assert 'SELECT COUNT(*) AS n FROM memberships WHERE organization_id=?' in t
    assert "role='OWNER'" in t
    assert "plan_limit(current_plan,'team_members')" in t
    assert "plan_limit(current_plan,'organizations')" in t


def test_billing_template_displays_all_usage_categories():
    t=(ROOT/'templates'/'billing.html').read_text(encoding='utf-8')
    assert 'UTILISATION CE MOIS-CI' in t
    assert 'quota_usage.imports.used' in t
    assert 'quota_usage.reports.used' in t
    assert 'quota_usage.team.used' in t
    assert 'quota_usage.organizations.used' in t
    assert 'Illimité' in t

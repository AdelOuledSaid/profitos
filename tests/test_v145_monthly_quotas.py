from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def test_usage_module_has_monthly_storage():
    t=(ROOT/'profitos'/'plan_usage.py').read_text(encoding='utf-8')
    assert 'CREATE TABLE IF NOT EXISTS plan_usage' in t
    assert 'PRIMARY KEY (organization_id, period, metric)' in t
    assert 'strftime("%Y-%m")' in t
    assert 'ON CONFLICT(organization_id,period,metric)' in t


def test_import_routes_enforce_and_record_monthly_quota():
    t=(ROOT/'profitos'/'routes'/'imports.py').read_text(encoding='utf-8')
    assert t.count("quota_state('imports_per_month'") >= 2
    assert t.count("record_usage('imports_per_month'") >= 2
    assert "Quota mensuel d'imports atteint" in t


def test_report_routes_enforce_and_record_monthly_quota():
    t=(ROOT/'profitos'/'routes'/'reports.py').read_text(encoding='utf-8')
    assert t.count("quota_state('reports_per_month'") >= 2
    assert t.count("record_usage('reports_per_month'") >= 2
    assert "Quota mensuel de rapports atteint" in t


def test_weekly_preview_does_not_consume_quota():
    t=(ROOT/'profitos'/'routes'/'reports.py').read_text(encoding='utf-8')
    preview=t.split('def weekly_report_preview():',1)[1].split('def pdf_safe',1)[0]
    assert 'record_usage(' not in preview


def test_weekly_send_counts_only_real_send():
    t=(ROOT/'profitos'/'routes'/'reports.py').read_text(encoding='utf-8')
    block=t.split('def weekly_report_send():',1)[1].split("@app.route('/impact'",1)[0]
    assert "if result.get('sent')" in block
    assert "record_usage('reports_per_month'" in block
    dry=block.split("elif result.get('dry_run')",1)[1]
    assert "record_usage('reports_per_month'" not in dry

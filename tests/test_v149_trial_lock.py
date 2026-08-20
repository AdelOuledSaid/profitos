from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(*parts):
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def test_paid_plan_helper_only_accepts_paid_plans():
    t=_read("profitos","feature_access.py")
    assert 'PAID_PLANS = {"STARTER", "PRO", "BUSINESS"}' in t
    assert 'def requires_paid_plan(fn):' in t
    assert 'TRIAL/FREE sont bloqués' in t


def test_imports_are_paid_only():
    t=_read("profitos","routes","imports.py")
    invoice=t.split("def upload_invoices():",1)[0][-400:]
    expense=t.split("def upload_expenses():",1)[0][-400:]
    assert "@requires_paid_plan" in invoice
    assert "@requires_paid_plan" in expense


def test_reports_generation_and_send_are_paid_only_but_preview_is_not():
    t=_read("profitos","routes","reports.py")
    pdf=t.split("def monthly_report_pdf():",1)[0][-400:]
    send=t.split("def weekly_report_send():",1)[0][-400:]
    preview=t.split("def weekly_report_preview():",1)[0][-300:]
    assert "@requires_paid_plan" in pdf
    assert "@requires_paid_plan" in send
    assert "@requires_paid_plan" not in preview


def test_grow_is_paid_only():
    t=_read("profitos","routes","main.py")
    grow=t.split("def grow():",1)[0][-500:]
    refresh=t.split("def grow_refresh():",1)[0][-500:]
    assert "@requires_paid_plan" in grow
    assert "@requires_paid_plan" in refresh


def test_direct_grow_detail_and_status_are_blocked_for_trial():
    t=_read("profitos","routes","main.py")
    assert "if kind=='GROW' and not current_plan_is_paid()" in t
    assert "_deny_paid_feature('grow')" in t
    assert "_deny_paid_feature('grow_status')" in t


def test_actions_workflow_is_paid_only():
    t=_read("profitos","routes","actions.py")
    assert t.count("@requires_paid_plan") >= 4


def test_dce_is_paid_and_advanced_only():
    t=_read("profitos","routes","dce.py")
    block=t.split("def dce_upload(opportunity_id):",1)[0][-500:]
    assert "@requires_paid_plan" in block
    assert "@requires_feature('advanced_features')" in block


def test_trial_still_keeps_dashboard_recover_save_company():
    t=_read("profitos","routes","main.py")
    for fn in ("home","company","recover","save"):
        block=t.split(f"def {fn}(",1)[0][-450:]
        assert "@requires_paid_plan" not in block

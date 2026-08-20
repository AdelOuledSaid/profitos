from pathlib import Path
import importlib.util

ROOT=Path(__file__).resolve().parents[1]


def _load_limits():
    p=ROOT/"profitos"/"plan_limits.py"
    spec=importlib.util.spec_from_file_location("profitos_plan_limits_test",p)
    m=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_plan_limits_values():
    m=_load_limits()
    assert m.plan_limit("STARTER","team_members")==2
    assert m.plan_limit("PRO","team_members")==10
    assert m.plan_limit("BUSINESS","team_members")==50
    assert m.plan_limit("STARTER","imports_per_month")==20
    assert m.plan_limit("PRO","imports_per_month")==200
    assert m.plan_limit("BUSINESS","imports_per_month") is None
    assert m.plan_limit("STARTER","reports_per_month")==10
    assert m.plan_limit("PRO","reports_per_month")==100
    assert m.plan_limit("BUSINESS","reports_per_month") is None


def test_within_limit_and_unlimited():
    m=_load_limits()
    assert m.within_limit("STARTER","team_members",1,1)
    assert not m.within_limit("STARTER","team_members",2,1)
    assert m.within_limit("BUSINESS","imports_per_month",999999,1)


def test_advanced_features():
    m=_load_limits()
    assert not m.feature_enabled("STARTER","advanced_features")
    assert m.feature_enabled("PRO","advanced_features")
    assert m.feature_enabled("BUSINESS","advanced_features")


def test_account_enforces_team_and_org_limits():
    t=(ROOT/"profitos"/"routes"/"account.py").read_text(encoding="utf-8")
    assert "within_limit(active_plan,'organizations'" in t
    assert "within_limit(plan,'team_members'" in t
    assert "PLAN_LIMIT" in t
    assert "plan_limits=PLAN_LIMITS" in t

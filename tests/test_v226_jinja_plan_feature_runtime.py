from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = (ROOT / "profitos" / "runtime.py").read_text(encoding="utf-8")
BASE = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")


def test_plan_feature_helper_uses_existing_entitlement_engine():
    assert "from .plan_limits import feature_enabled as plan_feature_enabled" in RUNTIME


def test_plan_feature_helper_is_registered_in_jinja():
    assert "app.jinja_env.globals['plan_feature_enabled'] = plan_feature_enabled" in RUNTIME


def test_base_navigation_feature_guards_have_runtime_helper():
    for feature in ("accounting_core", "banking", "advanced_ai", "multi_entity"):
        assert f"plan_feature_enabled(auth_org.plan,'{feature}')" in BASE
    assert "app.jinja_env.globals['plan_feature_enabled'] = plan_feature_enabled" in RUNTIME

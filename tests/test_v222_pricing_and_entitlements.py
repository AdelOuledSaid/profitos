from pathlib import Path
import importlib.util

ROOT = Path(__file__).resolve().parents[1]


def _limits():
    p = ROOT / "profitos" / "plan_limits.py"
    spec = importlib.util.spec_from_file_location("p43_limits", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_pass43_prices_and_multi_stripe_plan():
    t = (ROOT / "profitos" / "runtime.py").read_text(encoding="utf-8")
    for code, price in [("STARTER", 9), ("PRO", 19), ("BUSINESS", 49), ("MULTI", 199)]:
        assert f"'{code}':" in t
        assert f"'price_eur':{price}" in t
    assert "STRIPE_PRICE_MULTI_ID" in t


def test_pass43_entitlement_matrix():
    m = _limits()
    assert not m.feature_enabled("STARTER", "accounting_core")
    assert m.feature_enabled("PRO", "accounting_core")
    assert not m.feature_enabled("STARTER", "banking")
    assert m.feature_enabled("PRO", "banking")
    assert not m.feature_enabled("PRO", "advanced_ai")
    assert m.feature_enabled("BUSINESS", "advanced_ai")
    assert not m.feature_enabled("BUSINESS", "multi_entity")
    assert m.feature_enabled("MULTI", "multi_entity")


def test_pass43_backend_routes_are_gated():
    accounting = (ROOT / "profitos/routes/accounting.py").read_text(encoding="utf-8")
    banking = (ROOT / "profitos/routes/bank_sync.py").read_text(encoding="utf-8")
    brain = (ROOT / "profitos/routes/financial_brain.py").read_text(encoding="utf-8")
    cash = (ROOT / "profitos/routes/cash_intelligence.py").read_text(encoding="utf-8")
    simulator = (ROOT / "profitos/routes/decision_simulator.py").read_text(encoding="utf-8")
    entities = (ROOT / "profitos/routes/entities.py").read_text(encoding="utf-8")
    assert "@requires_feature('accounting_core')" in accounting
    assert "@requires_feature('banking')" in banking
    assert "@requires_feature('advanced_ai')" in brain
    assert "@requires_feature('advanced_ai')" in cash
    assert "@requires_feature('advanced_ai')" in simulator
    assert entities.count("@requires_feature('multi_entity')") >= 2


def test_pass43_public_pricing_shows_four_offers():
    t = (ROOT / "templates/pricing.html").read_text(encoding="utf-8")
    assert "Starter<br>9 €" in t
    assert "Pro<br>19 €" in t
    assert "Business<br>49 €" in t
    assert "Multi-Entity<br>199 €" in t


def test_pass43_navigation_hides_locked_modules():
    t = (ROOT / "templates/base.html").read_text(encoding="utf-8")
    assert "plan_feature_enabled(auth_org.plan,'accounting_core')" in t
    assert "plan_feature_enabled(auth_org.plan,'banking')" in t
    assert "plan_feature_enabled(auth_org.plan,'advanced_ai')" in t
    assert "plan_feature_enabled(auth_org.plan,'multi_entity')" in t

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_billing_advanced_ai_is_conditional():
    text = (ROOT / "templates" / "billing.html").read_text(encoding="utf-8")
    assert "plan_limits[code].advanced_ai" in text
    assert "Diagnostic financier et trésorerie" in text
    assert "Simulateur de décision" in text
    assert "{{ '✓' if plan_limits[code].advanced_ai else '—' }}" in text


def test_billing_does_not_claim_advanced_ai_for_every_plan():
    text = (ROOT / "templates" / "billing.html").read_text(encoding="utf-8")
    assert '<div class="billing-feature"><span class="tick">✓</span><span>Financial Brain & Cash Intelligence</span></div>' not in text
    assert '<div class="billing-feature"><span class="tick">✓</span><span>AI CFO Planner & Cash Forecast</span></div>' not in text

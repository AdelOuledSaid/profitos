from pathlib import Path

ACCOUNT = Path("profitos/routes/account.py")


def _text():
    return ACCOUNT.read_text(encoding="utf-8")


def test_billing_checkout_owner_and_rate_limit():
    t=_text()
    assert "@rate_limit(6,60)" in t
    assert "current_role()!='OWNER'" in t
    assert "BILLING_CHECKOUT" in t


def test_billing_checkout_has_anti_double_click_and_idempotency():
    t=_text()
    assert "billing_checkout_lock_" in t
    assert "idempotency_key=f\"profitos-customer-org-" in t
    assert "idempotency_key=f\"profitos-checkout-" in t


def test_billing_portal_is_hardened():
    t=_text()
    assert "@rate_limit(10,300)" in t
    assert "BILLING_PORTAL" in t


def test_webhook_rejects_wrong_mode_and_large_payloads():
    t=_text()
    assert "payload too large" in t
    assert "mode mismatch" in t
    assert "startswith('sk_live_')" in t


def test_webhook_checks_checkout_customer_ownership():
    t=_text()
    assert "Stripe checkout customer mismatch" in t
    assert "stripe_customer_id']==customer_id" in t

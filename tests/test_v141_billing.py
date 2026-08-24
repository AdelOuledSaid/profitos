from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_version_141():
    assert 'APP_VERSION = "1.6.2"' in (ROOT/'profitos/config.py').read_text(encoding='utf-8')

def test_three_server_side_plans_and_signed_webhook():
    t=(ROOT/'profitos/runtime.py').read_text(encoding='utf-8')
    assert 'STRIPE_PRICE_STARTER_ID' in t and 'STRIPE_PRICE_PRO_ID' in t and 'STRIPE_PRICE_BUSINESS_ID' in t
    assert 'STRIPE_WEBHOOK_SECRET and STRIPE_PRICE_TO_PLAN' in t

def test_webhook_idempotency_and_payment_events():
    t=(ROOT/'profitos/routes/account.py').read_text(encoding='utf-8')
    assert 'stripe_webhook_events' in t
    assert "invoice.payment_failed" in t and "invoice.payment_succeeded" in t
    assert "customer.subscription.updated" in t and "customer.subscription.deleted" in t

def test_browser_success_does_not_activate_plan():
    t=(ROOT/'profitos/routes/account.py').read_text(encoding='utf-8')
    block=t[t.index("def billing_success") : t.index("@app.route('/billing/webhook")]
    assert "UPDATE organizations" not in block

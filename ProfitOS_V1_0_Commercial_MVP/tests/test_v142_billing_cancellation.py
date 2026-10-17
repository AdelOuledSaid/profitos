from pathlib import Path


def test_billing_tracks_stripe_cancel_at_period_end():
    t = Path("profitos/routes/account.py").read_text(encoding="utf-8")
    assert "cancel_at_period_end" in t
    assert "current_period_end" in t
    assert "Annulation programmée" in t
    assert "Stripe subscription status lookup failure" in t


def test_billing_keeps_webhook_as_source_of_truth():
    t = Path("profitos/routes/account.py").read_text(encoding="utf-8")
    assert "customer.subscription.updated" in t
    assert "customer.subscription.deleted" in t
    assert "status='CANCELED'" in t
    assert "status='ACTIVE_PAID'" in t

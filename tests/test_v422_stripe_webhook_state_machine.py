"""V4 — webhook Stripe : seul l'abonnement enregistré fait foi (item 4 et 7)."""
from v4_billing_support import (  # noqa: F401
    billing_env, make_org, org_row, post_event, sub_obj, PRICE_IDS,
)


def test_payment_failed_does_not_revive_canceled_org(billing_env):
    oid = make_org(plan="PRO", status="CANCELED", trial_days=None, customer="cus_1", subscription="sub_1")
    client = billing_env.app.test_client()
    r = post_event(client, "invoice.payment_failed", {"customer": "cus_1", "subscription": "sub_1"})
    assert r.status_code == 200
    assert org_row(oid)["status"] == "CANCELED"


def test_payment_failed_moves_active_to_past_due_with_grace_start(billing_env):
    oid = make_org(plan="PRO", status="ACTIVE_PAID", trial_days=None, customer="cus_1", subscription="sub_1")
    client = billing_env.app.test_client()
    post_event(client, "invoice.payment_failed", {"customer": "cus_1", "subscription": "sub_1"})
    row = org_row(oid)
    assert row["status"] == "PAST_DUE" and row["past_due_since"]
    post_event(client, "invoice.payment_succeeded", {"customer": "cus_1", "subscription": "sub_1"})
    row = org_row(oid)
    assert row["status"] == "ACTIVE_PAID" and not row["past_due_since"]


def test_payment_failed_for_other_subscription_is_ignored(billing_env):
    oid = make_org(plan="PRO", status="ACTIVE_PAID", trial_days=None, customer="cus_1", subscription="sub_1")
    client = billing_env.app.test_client()
    post_event(client, "invoice.payment_failed", {"customer": "cus_1", "subscription": "sub_old"})
    assert org_row(oid)["status"] == "ACTIVE_PAID"


def test_deleted_event_for_old_subscription_does_not_cancel_paying_org(billing_env):
    oid = make_org(plan="PRO", status="ACTIVE_PAID", trial_days=None, customer="cus_1", subscription="sub_new")
    client = billing_env.app.test_client()
    post_event(client, "customer.subscription.deleted", sub_obj("sub_old", "canceled", org_id=oid))
    assert org_row(oid)["status"] == "ACTIVE_PAID"
    assert org_row(oid)["stripe_subscription_id"] == "sub_new"
    post_event(client, "customer.subscription.deleted", sub_obj("sub_new", "canceled", org_id=oid))
    assert org_row(oid)["status"] == "CANCELED"


def test_late_incomplete_created_event_never_downgrades_active(billing_env):
    oid = make_org(plan="PRO", status="ACTIVE_PAID", trial_days=None, customer="cus_1", subscription="sub_1")
    billing_env.stripe.subscriptions["sub_1"] = sub_obj("sub_1", "active", org_id=oid)
    client = billing_env.app.test_client()
    post_event(client, "customer.subscription.created", sub_obj("sub_1", "incomplete", org_id=oid))
    assert org_row(oid)["status"] == "ACTIVE_PAID"
    # Le handler relit l'abonnement depuis l'API (mockée).
    assert billing_env.stripe.called("Subscription.retrieve")


def test_incomplete_status_never_sets_past_due(billing_env):
    oid = make_org(plan="PRO", status="ACTIVE_PAID", trial_days=None, customer="cus_1", subscription="sub_1")
    client = billing_env.app.test_client()
    post_event(client, "customer.subscription.updated", sub_obj("sub_1", "incomplete", org_id=oid))
    assert org_row(oid)["status"] == "ACTIVE_PAID"


def test_past_due_only_from_active_paid(billing_env):
    oid = make_org(plan="PRO", status="CANCELED", trial_days=None, customer="cus_1", subscription="sub_1")
    client = billing_env.app.test_client()
    post_event(client, "customer.subscription.updated", sub_obj("sub_1", "past_due", org_id=oid))
    assert org_row(oid)["status"] == "CANCELED"


def test_first_attachment_via_subscription_created(billing_env):
    oid = make_org(customer="cus_1")
    client = billing_env.app.test_client()
    post_event(client, "customer.subscription.created",
               sub_obj("sub_9", "trialing", price=PRICE_IDS["BUSINESS"], org_id=oid))
    row = org_row(oid)
    assert row["status"] == "ACTIVE_PAID" and row["plan"] == "BUSINESS"
    assert row["stripe_subscription_id"] == "sub_9"


def test_subscription_event_for_unknown_subscription_with_stored_one_is_ignored(billing_env):
    oid = make_org(plan="PRO", status="ACTIVE_PAID", trial_days=None, customer="cus_1", subscription="sub_1")
    client = billing_env.app.test_client()
    post_event(client, "customer.subscription.updated",
               sub_obj("sub_other", "active", price=PRICE_IDS["STARTER"], org_id=oid))
    row = org_row(oid)
    assert row["plan"] == "PRO" and row["stripe_subscription_id"] == "sub_1"


def test_unknown_price_keeps_current_plan_and_ignores_stale_metadata(billing_env):
    oid = make_org(plan="BUSINESS", status="ACTIVE_PAID", trial_days=None, customer="cus_1", subscription="sub_1")
    obj = sub_obj("sub_1", "active", price="price_inconnu", org_id=oid)
    obj["metadata"]["plan"] = "MULTI"
    client = billing_env.app.test_client()
    post_event(client, "customer.subscription.updated", obj)
    assert org_row(oid)["plan"] == "BUSINESS"


def test_plan_change_in_portal_updates_plan_from_price(billing_env):
    oid = make_org(plan="PRO", status="ACTIVE_PAID", trial_days=None, customer="cus_1", subscription="sub_1")
    billing_env.stripe.subscriptions["sub_1"] = sub_obj("sub_1", "active", price=PRICE_IDS["STARTER"], org_id=oid)
    client = billing_env.app.test_client()
    post_event(client, "customer.subscription.updated", sub_obj("sub_1", "active", price=PRICE_IDS["STARTER"], org_id=oid))
    assert org_row(oid)["plan"] == "STARTER"


def test_second_checkout_for_already_paying_org_is_not_attached(billing_env):
    oid = make_org(plan="PRO", status="ACTIVE_PAID", trial_days=None, customer="cus_1", subscription="sub_1")
    client = billing_env.app.test_client()
    post_event(client, "checkout.session.completed", {
        "subscription": "sub_dup", "customer": "cus_1", "client_reference_id": str(oid),
        "metadata": {"organization_id": str(oid), "plan": "BUSINESS"}})
    row = org_row(oid)
    assert row["stripe_subscription_id"] == "sub_1" and row["plan"] == "PRO"


def test_checkout_completed_uses_price_from_refetched_subscription(billing_env):
    oid = make_org(customer="cus_1")
    billing_env.stripe.subscriptions["sub_5"] = sub_obj("sub_5", "trialing", price=PRICE_IDS["MULTI"], org_id=oid)
    client = billing_env.app.test_client()
    post_event(client, "checkout.session.completed", {
        "subscription": "sub_5", "customer": "cus_1", "client_reference_id": str(oid),
        "metadata": {"organization_id": str(oid), "plan": "PRO"}})
    row = org_row(oid)
    assert row["status"] == "ACTIVE_PAID" and row["plan"] == "MULTI" and row["stripe_subscription_id"] == "sub_5"


def test_parent_status_propagates_to_child_orgs(billing_env):
    parent = make_org(plan="MULTI", status="ACTIVE_PAID", trial_days=None, customer="cus_1", subscription="sub_1")
    child = make_org(name="Filiale", plan="MULTI", status="ACTIVE_PAID", trial_days=None, parent=parent)
    client = billing_env.app.test_client()
    post_event(client, "customer.subscription.deleted", sub_obj("sub_1", "canceled", price=PRICE_IDS["MULTI"]))
    assert org_row(child)["status"] == "CANCELED"


def test_checkout_with_unknown_price_does_not_trust_session_metadata(billing_env):
    oid = make_org(plan="TRIAL", status="ACTIVE", customer="cus_1")
    billing_env.stripe.subscriptions["sub_x"] = sub_obj("sub_x", "active", price="price_inconnu", org_id=oid)
    client = billing_env.app.test_client()
    post_event(client, "checkout.session.completed", {
        "subscription": "sub_x", "customer": "cus_1", "client_reference_id": str(oid),
        "payment_status": "paid", "metadata": {"organization_id": str(oid), "plan": "MULTI"}})
    row = org_row(oid)
    assert row["plan"] == "TRIAL" and row["status"] == "ACTIVE"
    assert row["stripe_subscription_id"] == "sub_x"  # rattaché, sans formule indéterminée


def test_unpaid_sepa_checkout_waits_for_subscription_update(billing_env):
    oid = make_org(plan="TRIAL", status="ACTIVE", customer="cus_1")
    sub = sub_obj("sub_s", "active", price=PRICE_IDS["PRO"], org_id=oid)
    billing_env.stripe.subscriptions["sub_s"] = sub
    client = billing_env.app.test_client()
    post_event(client, "checkout.session.completed", {
        "subscription": "sub_s", "customer": "cus_1", "client_reference_id": str(oid),
        "payment_status": "unpaid", "metadata": {"organization_id": str(oid), "plan": "PRO"}})
    assert org_row(oid)["status"] == "ACTIVE" and org_row(oid)["plan"] == "TRIAL"
    post_event(client, "customer.subscription.updated", sub)
    row = org_row(oid)
    assert row["status"] == "ACTIVE_PAID" and row["plan"] == "PRO" and row["stripe_subscription_id"] == "sub_s"

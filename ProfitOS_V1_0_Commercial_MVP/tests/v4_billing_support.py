"""Outils partagés par les tests V4 (abonnement, facturation, contrôle d'accès).

Chaque test obtient une base auth SQLite et des bases tenant isolées dans un
répertoire temporaire, et un faux module Stripe (aucun appel réseau)."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from profitos import create_app, runtime
from profitos.config import DevelopmentConfig


PRICE_IDS = {
    "STARTER": "price_starter_test",
    "PRO": "price_pro_test",
    "BUSINESS": "price_business_test",
    "MULTI": "price_multi_test",
}


class FakeStripe:
    """Faux module stripe enregistrant les appels."""

    def __init__(self):
        self.calls = []
        self.subscriptions = {}        # id -> objet abonnement (relu par retrieve)
        self.customer_subscriptions = []  # retourné par Subscription.list
        self.open_sessions = []
        self.event = None
        fake = self

        class Subscription:
            @staticmethod
            def retrieve(sub_id):
                fake.calls.append(("Subscription.retrieve", sub_id))
                if sub_id not in fake.subscriptions:
                    raise RuntimeError("No such subscription")
                return fake.subscriptions[sub_id]

            @staticmethod
            def list(**kw):
                fake.calls.append(("Subscription.list", kw))
                return {"data": list(fake.customer_subscriptions)}

        class Customer:
            @staticmethod
            def retrieve(cid):
                fake.calls.append(("Customer.retrieve", cid))
                return {"id": cid}

            @staticmethod
            def create(**kw):
                fake.calls.append(("Customer.create", kw))
                return {"id": "cus_new"}

            @staticmethod
            def create_balance_transaction(cid, **kw):
                fake.calls.append(("Customer.create_balance_transaction", cid, kw))
                return {"id": "cbtxn_1"}

        class CheckoutSession:
            @staticmethod
            def create(**kw):
                fake.calls.append(("checkout.Session.create", kw))
                return SimpleNamespace(url="https://checkout.stripe.com/c/pay/test", id="cs_new")

            @staticmethod
            def list(**kw):
                fake.calls.append(("checkout.Session.list", kw))
                return {"data": list(fake.open_sessions)}

            @staticmethod
            def expire(sid):
                fake.calls.append(("checkout.Session.expire", sid))
                return {"id": sid, "status": "expired"}

        class PortalSession:
            @staticmethod
            def create(**kw):
                fake.calls.append(("billing_portal.Session.create", kw))
                return SimpleNamespace(url="https://billing.stripe.com/p/session/test")

        class Webhook:
            @staticmethod
            def construct_event(payload, sig, secret):
                return json.loads(payload)

        self.Subscription = Subscription
        self.Customer = Customer
        self.checkout = SimpleNamespace(Session=CheckoutSession)
        self.billing_portal = SimpleNamespace(Session=PortalSession)
        self.Webhook = Webhook

    def called(self, name):
        return [c for c in self.calls if c[0] == name]


def sub_obj(sub_id, status, price=PRICE_IDS["PRO"], org_id=None, customer="cus_1"):
    return {
        "id": sub_id,
        "object": "subscription",
        "status": status,
        "customer": customer,
        "metadata": {"organization_id": str(org_id)} if org_id else {},
        "items": {"data": [{"price": {"id": price}}]},
    }


@pytest.fixture
def billing_env(tmp_path, monkeypatch):
    """Application isolée, Stripe « configuré » avec un faux module."""
    from profitos.routes import account

    monkeypatch.setattr(runtime, "AUTH_DB", tmp_path / "auth.db")
    monkeypatch.setattr(runtime, "TENANTS", tmp_path)
    monkeypatch.setattr(runtime, "_tenant_schema_checked", set())
    monkeypatch.setenv("PROFITOS_ENV", "test")
    fake = FakeStripe()
    for code, pid in PRICE_IDS.items():
        monkeypatch.setitem(runtime.STRIPE_PLANS[code], "price_id", pid)
        monkeypatch.setitem(runtime.STRIPE_PRICE_TO_PLAN, pid, code)
    app = create_app(DevelopmentConfig)
    app.config.update(TESTING=True)
    for mod in (runtime, account):
        monkeypatch.setattr(mod, "BILLING_ENABLED", True)
        monkeypatch.setattr(mod, "get_stripe", lambda: fake)
    return SimpleNamespace(app=app, stripe=fake, tmp=tmp_path)


def iso(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def make_org(name="Acme", plan="TRIAL", status="ACTIVE", trial_days=10, parent=None,
             customer=None, subscription=None, past_due_since=None, referral_code=None):
    c = runtime.auth_cx()
    slug = f"{name.lower().replace(' ', '-')}-{datetime.now().timestamp()}"
    c.execute(
        "INSERT INTO organizations(name,slug,plan,status,trial_ends_at,stripe_customer_id,"
        "stripe_subscription_id,parent_org_id,past_due_since,referral_code,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, slug, plan, status, iso(trial_days) if trial_days is not None else None,
         customer, subscription, parent, past_due_since, referral_code, runtime.now(), runtime.now()),
    )
    oid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.commit(); c.close()
    runtime.init_tenant_db(oid)
    return oid


def make_user(org_id, role="OWNER", email=None, name="Utilisateur"):
    c = runtime.auth_cx()
    email = email or f"{role.lower()}-{org_id}-{datetime.now().timestamp()}@example.fr"
    c.execute(
        "INSERT INTO users(email,password_hash,full_name,is_active,email_verified,created_at,updated_at) "
        "VALUES(?,?,?,1,1,?,?)", (email, "x", name, runtime.now(), runtime.now()))
    uid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute("INSERT INTO memberships(user_id,organization_id,role,created_at) VALUES(?,?,?,?)",
              (uid, org_id, role, runtime.now()))
    c.commit(); c.close()
    return uid


def login(client, uid, org_id, role="OWNER"):
    with client.session_transaction() as s:
        s["user_id"] = uid
        s["org_id"] = org_id
        s["role"] = role
        s["auth_version"] = 0
        s["csrf_token"] = "csrf-test"


def org_row(org_id):
    c = runtime.auth_cx()
    r = c.execute("SELECT * FROM organizations WHERE id=?", (org_id,)).fetchone()
    c.close()
    return r


def post_event(client, event_type, obj, event_id=None):
    payload = {"id": event_id or f"evt_{datetime.now().timestamp()}", "type": event_type,
               "livemode": False, "data": {"object": obj}}
    return client.post("/billing/webhook", data=json.dumps(payload),
                       headers={"Stripe-Signature": "t=1,v1=test"}, content_type="application/json")

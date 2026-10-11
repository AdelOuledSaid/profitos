"""V4 — formule Multi-sociétés : organisations rattachées au parent payeur (item 6)."""
import sqlite3

from v4_billing_support import billing_env, make_org, make_user, login, org_row  # noqa: F401

from profitos import runtime


def _create_org(client, name):
    return client.post("/organizations/new", data={"csrf_token": "csrf-test", "org_name": name})


def _orgs_named(name):
    c = runtime.auth_cx()
    rows = c.execute("SELECT * FROM organizations WHERE name=?", (name,)).fetchall()
    c.close()
    return rows


def test_org_created_from_multi_is_attached_to_paying_parent(billing_env):
    parent = make_org(plan="MULTI", status="ACTIVE_PAID", trial_days=None, customer="cus_1", subscription="sub_1")
    owner = make_user(parent)
    client = billing_env.app.test_client()
    login(client, owner, parent)
    r = _create_org(client, "Filiale Sud")
    assert r.status_code == 302
    (child,) = _orgs_named("Filiale Sud")
    assert child["parent_org_id"] == parent
    assert child["plan"] == "MULTI" and child["status"] == "ACTIVE_PAID"
    assert child["trial_ends_at"] is None
    # L'accès et le plan de la fille suivent le parent, même après 14 jours.
    assert runtime.org_has_access(child)
    assert runtime.effective_plan(child) == "MULTI"
    # Depuis la fille (désormais active), on peut créer d'autres sociétés.
    r = _create_org(client, "Filiale Nord")
    (child2,) = _orgs_named("Filiale Nord")
    assert child2["parent_org_id"] == parent


def test_child_access_follows_parent_cancellation(billing_env):
    parent = make_org(plan="MULTI", status="ACTIVE_PAID", trial_days=None)
    child = make_org(name="Fille", plan="MULTI", status="ACTIVE_PAID", trial_days=None, parent=parent)
    c = runtime.auth_cx()
    c.execute("UPDATE organizations SET status='CANCELED' WHERE id=?", (parent,))
    c.commit(); c.close()
    assert runtime.org_has_access(org_row(child)) is False


def test_orphan_child_never_keeps_inherited_access(billing_env):
    child = make_org(name="Orpheline", plan="MULTI", status="ACTIVE_PAID", trial_days=None, parent=99999)
    assert runtime.org_has_access(org_row(child)) is False


def test_multi_org_limit_counts_parent_and_children(billing_env, monkeypatch):
    from profitos import plan_limits
    monkeypatch.setitem(plan_limits.PLAN_LIMITS["MULTI"], "organizations", 2)
    parent = make_org(plan="MULTI", status="ACTIVE_PAID", trial_days=None)
    owner = make_user(parent)
    make_org(name="Fille 1", plan="MULTI", status="ACTIVE_PAID", trial_days=None, parent=parent)
    client = billing_env.app.test_client()
    login(client, owner, parent)
    _create_org(client, "Fille 2")
    assert _orgs_named("Fille 2") == []


def test_non_multi_plan_keeps_independent_trial_and_limit(billing_env):
    oid = make_org(plan="PRO", status="ACTIVE_PAID", trial_days=None)
    owner = make_user(oid)
    client = billing_env.app.test_client()
    login(client, owner, oid)
    _create_org(client, "Deuxième")
    assert _orgs_named("Deuxième") == []  # PRO : 1 organisation


def test_migration_adds_billing_columns_to_existing_db(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE organizations(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,slug TEXT UNIQUE,"
              "plan TEXT DEFAULT 'TRIAL',status TEXT DEFAULT 'ACTIVE',trial_ends_at TEXT,created_at TEXT,updated_at TEXT)")
    c.commit(); c.close()
    monkeypatch.setattr(runtime, "AUTH_DB", db)
    runtime.init_auth_db()
    c = sqlite3.connect(db)
    cols = {r[1] for r in c.execute("PRAGMA table_info(organizations)")}
    c.close()
    assert {"parent_org_id", "past_due_since", "referral_credit_months"} <= cols
    src = (runtime.BASE / "profitos" / "runtime.py").read_text(encoding="utf-8")
    # Même mécanisme de migration douce que les autres colonnes (traduit pour PostgreSQL par profitos/db.py).
    assert "ALTER TABLE organizations ADD COLUMN parent_org_id INTEGER" in src

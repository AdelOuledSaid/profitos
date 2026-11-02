"""R?gression : les liens publics survivent ? un import ?chou?."""
import io
from unittest.mock import patch

import pytest

from support_v4xx import app, signup, post
from profitos import runtime as rt
from profitos.routes import imports as imports_routes


def _upload(client, number):
    csv = (
        "invoice_number;customer;amount;paid_amount;issue_date;due_date\n"
        f"{number};Client Test;100;0;2026-01-01;2026-02-01\n"
    )
    return post(
        client,
        "/upload/invoices",
        {"file": (io.BytesIO(csv.encode()), "factures.csv")},
        content_type="multipart/form-data",
    )


def test_failed_import_preserves_previous_public_link(app):
    client, _, org_id, _ = signup(
        app, company="Audit Import Tokens", plan="STARTER"
    )

    response = _upload(client, "FACTURE-INITIALE")
    from support_v4xx import flashes
    print("IMPORT HTTP :", response.status_code)
    print("MESSAGES :", flashes(client))


    c = rt.tenant_cx_direct(org_id)
    row = c.execute(
        "SELECT id, public_token FROM invoices WHERE invoice_number=?",
        ("FACTURE-INITIALE",),
    ).fetchone()
    c.close()

    assert row is not None
    old_token = row["public_token"]
    assert app.test_client().get(f"/pay-status/{old_token}").status_code == 200

    original = imports_routes.auth_cx

    def fail_after_registration():
        connection = original()
        original_execute = connection.execute

        def execute_with_failure(sql, *args, **kwargs):
            if "INSERT INTO public_invoice_tokens" in sql:
                result = original_execute(sql, *args, **kwargs)
                connection.commit()
                raise RuntimeError("PANNE SIMULEE APRES CREATION DU JETON")
            return original_execute(sql, *args, **kwargs)

        connection.execute = execute_with_failure
        return connection

    with patch.object(
        imports_routes,
        "auth_cx",
        side_effect=fail_after_registration,
    ):
        with pytest.raises(RuntimeError, match="PANNE SIMULEE"):
            _upload(client, "FACTURE-NOUVELLE")

    c = rt.tenant_cx_direct(org_id)
    old_invoice = c.execute(
        "SELECT id FROM invoices WHERE invoice_number=?",
        ("FACTURE-INITIALE",),
    ).fetchone()
    new_invoice = c.execute(
        "SELECT id FROM invoices WHERE invoice_number=?",
        ("FACTURE-NOUVELLE",),
    ).fetchone()
    c.close()

    assert old_invoice is not None
    assert new_invoice is None
    assert app.test_client().get(f"/pay-status/{old_token}").status_code == 200

    ac = rt.auth_cx()
    tokens = ac.execute(
        "SELECT token FROM public_invoice_tokens WHERE organization_id=?",
        (org_id,),
    ).fetchall()
    ac.close()
    assert {r["token"] for r in tokens} == {old_token}



def test_failure_after_token_commit(app):
    client, _, org_id, _ = signup(
        app, company="Audit After Commit", plan="STARTER"
    )
    _upload(client, "FACTURE-INITIALE")

    c = rt.tenant_cx_direct(org_id)
    old_token = c.execute(
        "SELECT public_token FROM invoices WHERE invoice_number=?",
        ("FACTURE-INITIALE",),
    ).fetchone()["public_token"]
    c.close()

    original = imports_routes.auth_cx

    def fail_after_commit():
        connection = original()
        original_commit = connection.commit

        def commit_with_failure():
            result = original_commit()
            raise RuntimeError("PANNE APRES COMMIT JETON")

        connection.commit = commit_with_failure
        return connection

    with patch.object(imports_routes, "auth_cx", side_effect=fail_after_commit):
        with pytest.raises(RuntimeError, match="PANNE APRES COMMIT JETON"):
            _upload(client, "FACTURE-NOUVELLE")

    c = rt.tenant_cx_direct(org_id)
    rows = c.execute(
        "SELECT invoice_number FROM invoices"
    ).fetchall()
    c.close()

    assert any(r["invoice_number"] == "FACTURE-INITIALE" for r in rows)
    assert not any(r["invoice_number"] == "FACTURE-NOUVELLE" for r in rows)

    ac = rt.auth_cx()
    tokens = ac.execute(
        "SELECT token FROM public_invoice_tokens WHERE organization_id=?",
        (org_id,),
    ).fetchall()
    ac.close()

    assert {r["token"] for r in tokens} == {old_token}


def test_failure_after_token_connection_closed(app):
    client, _, org_id, _ = signup(
        app, company="Audit Late Failure", plan="STARTER"
    )
    _upload(client, "FACTURE-INITIALE")

    c = rt.tenant_cx_direct(org_id)
    old_token = c.execute(
        "SELECT public_token FROM invoices WHERE invoice_number=?",
        ("FACTURE-INITIALE",),
    ).fetchone()["public_token"]
    c.close()

    original = imports_routes.secrets.token_urlsafe

    def fail_after_token_created(n):
        token = original(n)
        return token

    original_tenant = imports_routes.cx

    def failing_tenant_connection():
        connection = original_tenant()
        original_execute = connection.execute

        def execute_with_failure(sql, *args, **kwargs):
            if "UPDATE invoices SET public_token" in sql:
                raise RuntimeError("PANNE APRES INSERTION")
            return original_execute(sql, *args, **kwargs)

        connection.execute = execute_with_failure
        return connection

    with patch.object(imports_routes, "cx", side_effect=failing_tenant_connection):
        with pytest.raises(RuntimeError, match="PANNE APRES INSERTION"):
            _upload(client, "FACTURE-NOUVELLE")

    ac = rt.auth_cx()
    tokens = ac.execute(
        "SELECT token FROM public_invoice_tokens WHERE organization_id=?",
        (org_id,),
    ).fetchall()
    ac.close()

    assert {r["token"] for r in tokens} == {old_token}


def test_second_invoice_failure_rolls_back_all(app):
    client, _, org_id, _ = signup(
        app, company="Audit Multi Invoice", plan="STARTER"
    )
    _upload(client, "FACTURE-INITIALE")

    c = rt.tenant_cx_direct(org_id)
    old_token = c.execute(
        "SELECT public_token FROM invoices WHERE invoice_number=?",
        ("FACTURE-INITIALE",),
    ).fetchone()["public_token"]
    c.close()

    csv = chr(10).join([
        "invoice_number;customer;amount;paid_amount;issue_date;due_date",
        "FACTURE-A;Client A;100;0;2026-01-01;2026-02-01",
        "FACTURE-B;Client B;200;0;2026-01-01;2026-02-01",
        "",
    ])

    original = imports_routes.secrets.token_urlsafe
    counter = {"n": 0}

    def fail_second_token(n):
        counter["n"] += 1
        if counter["n"] == 2:
            raise RuntimeError("PANNE DEUXIEME FACTURE")
        return original(n)

    with patch.object(
        imports_routes.secrets,
        "token_urlsafe",
        side_effect=fail_second_token,
    ):
        with pytest.raises(RuntimeError, match="PANNE DEUXIEME FACTURE"):
            post(
                client,
                "/upload/invoices",
                {"file": (io.BytesIO(csv.encode()), "factures.csv")},
                content_type="multipart/form-data",
            )

    c = rt.tenant_cx_direct(org_id)
    numbers = {
        r["invoice_number"]
        for r in c.execute("SELECT invoice_number FROM invoices").fetchall()
    }
    c.close()

    assert numbers == {"FACTURE-INITIALE"}

    ac = rt.auth_cx()
    tokens = {
        r["token"]
        for r in ac.execute(
            "SELECT token FROM public_invoice_tokens WHERE organization_id=?",
            (org_id,),
        ).fetchall()
    }
    ac.close()

    assert tokens == {old_token}
    assert app.test_client().get(f"/pay-status/{old_token}").status_code == 200


def test_concurrent_invoice_imports_same_organization(app):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from support_v4xx import login

    _, email, org_id, _ = signup(
        app, company="Audit Concurrent Imports", plan="STARTER"
    )

    barrier = Barrier(2, timeout=10)

    def worker(number):
        client, response = login(app, email)
        assert response.status_code in (302, 303)
        barrier.wait()
        result = _upload(client, number)
        return number, result.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(worker, "FACTURE-CONCURRENT-A"),
            pool.submit(worker, "FACTURE-CONCURRENT-B"),
        ]
        results = [f.result(timeout=30) for f in futures]

    assert all(status in (302, 303) for _, status in results), results

    c = rt.tenant_cx_direct(org_id)
    try:
        rows = c.execute(
            "SELECT id,invoice_number,public_token FROM invoices"
        ).fetchall()
    finally:
        c.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["invoice_number"] in {
        "FACTURE-CONCURRENT-A", "FACTURE-CONCURRENT-B"
    }
    assert row["public_token"]

    ac = rt.auth_cx()
    try:
        tokens = ac.execute(
            "SELECT token,invoice_local_id FROM public_invoice_tokens "
            "WHERE organization_id=?",
            (org_id,),
        ).fetchall()
    finally:
        ac.close()

    assert len(tokens) == 1
    assert tokens[0]["token"] == row["public_token"]
    assert tokens[0]["invoice_local_id"] == row["id"]
    assert app.test_client().get(
        f"/pay-status/{row['public_token']}"
    ).status_code == 200


def test_orphan_public_token_cannot_access_invoice(app):
    import secrets

    client, _, org_id, _ = signup(
        app, company="Audit Orphan Tokens", plan="STARTER"
    )

    response = _upload(client, "FACTURE-VALIDE")
    assert response.status_code in (302, 303)

    tc = rt.tenant_cx_direct(org_id)
    try:
        invoice = tc.execute(
            "SELECT id, public_token FROM invoices "
            "WHERE invoice_number=?",
            ("FACTURE-VALIDE",),
        ).fetchone()
    finally:
        tc.close()

    assert invoice is not None
    valid_token = invoice["public_token"]
    orphan_token = secrets.token_urlsafe(20)

    ac = rt.auth_cx()
    try:
        ac.execute(
            "INSERT INTO public_invoice_tokens "
            "(token,organization_id,invoice_local_id,created_at) "
            "VALUES(?,?,?,?)",
            (orphan_token, org_id, invoice["id"], rt.now()),
        )
        ac.commit()
    finally:
        ac.close()

    public_client = app.test_client()

    assert public_client.get(
        f"/pay-status/{valid_token}"
    ).status_code == 200

    assert public_client.get(
        f"/pay-status/{orphan_token}"
    ).status_code == 404


def test_cleanup_old_orphan_tokens_preserves_valid_and_recent(app):
    import secrets
    from datetime import datetime, timedelta, timezone
    from profitos import runtime as rt

    client, _, org_id, _ = signup(
        app, company="Audit Token Cleanup", plan="STARTER"
    )

    response = _upload(client, "FACTURE-VALIDE")
    assert response.status_code in (302, 303)

    tc = rt.tenant_cx_direct(org_id)
    try:
        invoice = tc.execute(
            "SELECT id, public_token FROM invoices "
            "WHERE invoice_number=?",
            ("FACTURE-VALIDE",),
        ).fetchone()
    finally:
        tc.close()

    assert invoice is not None
    valid_token = invoice["public_token"]

    old_orphan = secrets.token_urlsafe(20)
    recent_orphan = secrets.token_urlsafe(20)

    old_date = (
        datetime.now(timezone.utc) - timedelta(days=3)
    ).isoformat()

    ac = rt.auth_cx()
    try:
        for token, created_at in (
            (old_orphan, old_date),
            (recent_orphan, rt.now()),
        ):
            ac.execute(
                "INSERT INTO public_invoice_tokens "
                "(token,organization_id,invoice_local_id,created_at) "
                "VALUES(?,?,?,?)",
                (token, org_id, invoice["id"], created_at),
            )
        ac.commit()
    finally:
        ac.close()

    from profitos.token_cleanup import cleanup_orphan_invoice_tokens

    cleanup_orphan_invoice_tokens(org_id, min_age_hours=24)

    ac = rt.auth_cx()
    try:
        rows = ac.execute(
            "SELECT token FROM public_invoice_tokens "
            "WHERE organization_id=?",
            (org_id,),
        ).fetchall()
    finally:
        ac.close()

    remaining = {row["token"] for row in rows}

    assert valid_token in remaining
    assert recent_orphan in remaining
    assert old_orphan not in remaining

    public_client = app.test_client()
    assert public_client.get(
        f"/pay-status/{valid_token}"
    ).status_code == 200


def test_cleanup_waits_for_tenant_transaction(app):
    import threading
    import time
    from profitos import runtime as rt
    from profitos.token_cleanup import cleanup_orphan_invoice_tokens

    _, _, org_id, _ = signup(
        app, company="Audit Cleanup Lock", plan="STARTER"
    )

    lock_holder = rt.tenant_cx_direct(org_id)
    lock_holder.execute("BEGIN IMMEDIATE")

    started = threading.Event()
    finished = threading.Event()
    errors = []

    def worker():
        started.set()
        try:
            cleanup_orphan_invoice_tokens(org_id)
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=worker, daemon=True)

    try:
        thread.start()
        assert started.wait(timeout=3)
        time.sleep(0.2)

        assert not finished.is_set(), (
            "Cleanup did not wait for the tenant transaction"
        )
    finally:
        lock_holder.rollback()
        lock_holder.close()

    thread.join(timeout=10)

    assert finished.is_set(), "Cleanup remained blocked"
    assert not errors, f"Cleanup failed: {errors}"


def test_failure_during_old_token_cleanup_after_invoice_commit(app):
    client, _, org_id, _ = signup(
        app, company="Audit Post Commit Cleanup", plan="STARTER"
    )

    assert _upload(client, "FACTURE-INITIALE").status_code in (302, 303)

    tc = rt.tenant_cx_direct(org_id)
    try:
        old_token = tc.execute(
            "SELECT public_token FROM invoices WHERE invoice_number=?",
            ("FACTURE-INITIALE",),
        ).fetchone()["public_token"]
    finally:
        tc.close()

    original_auth = imports_routes.auth_cx

    def fail_old_token_cleanup():
        connection = original_auth()
        original_execute = connection.execute

        def execute_with_failure(sql, *args, **kwargs):
            if (
                "DELETE FROM public_invoice_tokens" in sql
                and args
                and args[0]
                and args[0][0] == old_token
            ):
                raise RuntimeError("PANNE NETTOYAGE ANCIEN TOKEN")
            return original_execute(sql, *args, **kwargs)

        connection.execute = execute_with_failure
        return connection

    with patch.object(
        imports_routes, "auth_cx", side_effect=fail_old_token_cleanup
    ):
        with pytest.raises(RuntimeError, match="PANNE NETTOYAGE"):
            _upload(client, "FACTURE-NOUVELLE")

    tc = rt.tenant_cx_direct(org_id)
    try:
        rows = tc.execute(
            "SELECT invoice_number,public_token FROM invoices"
        ).fetchall()
    finally:
        tc.close()

    assert len(rows) == 1
    assert rows[0]["invoice_number"] == "FACTURE-NOUVELLE"
    new_token = rows[0]["public_token"]
    assert new_token and new_token != old_token

    assert app.test_client().get(
        f"/pay-status/{new_token}"
    ).status_code == 200

    assert app.test_client().get(
        f"/pay-status/{old_token}"
    ).status_code != 200

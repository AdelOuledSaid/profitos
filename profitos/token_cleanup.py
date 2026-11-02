
"""Conservative cleanup of stale public invoice token mappings."""

from datetime import datetime, timedelta, timezone

from profitos import runtime as rt


def _parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def cleanup_orphan_invoice_tokens(org_id, min_age_hours=24):
    """Remove stale mappings that no longer match an invoice.

    Run only as a controlled maintenance operation, not during imports.
    """
    if min_age_hours < 24:
        raise ValueError("Minimum age must be at least 24 hours")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=min_age_hours)
    tc = rt.tenant_cx_direct(org_id)
    ac = None
    removed = 0

    try:
        tc.execute("BEGIN IMMEDIATE")
        ac = rt.auth_cx()
        mappings = ac.execute(
            "SELECT token,invoice_local_id,created_at "
            "FROM public_invoice_tokens WHERE organization_id=?",
            (org_id,),
        ).fetchall()

        for mapping in mappings:
                created = _parse_datetime(mapping["created_at"])
                if created is None or created > cutoff:
                    continue

                invoice = tc.execute(
                    "SELECT id FROM invoices "
                    "WHERE id=? AND public_token=?",
                    (mapping["invoice_local_id"], mapping["token"]),
                ).fetchone()

                if invoice is not None:
                    continue

                ac.execute(
                    "DELETE FROM public_invoice_tokens "
                    "WHERE organization_id=? AND token=?",
                    (org_id, mapping["token"]),
                )
                removed += 1
        ac.commit()
        tc.commit()
        return removed
    except Exception:
        if ac is not None:
            ac.rollback()
        tc.rollback()
        raise
    finally:
        if ac is not None:
            ac.close()
        tc.close()

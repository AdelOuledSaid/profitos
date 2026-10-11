from datetime import datetime, timezone

from profitos.runtime import auth_cx, current_org, now
from profitos.plan_limits import plan_limit


def _month_key():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _ensure_usage_table(c):
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS plan_usage (
            organization_id INTEGER NOT NULL,
            period TEXT NOT NULL,
            metric TEXT NOT NULL,
            usage_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (organization_id, period, metric)
        )
        """
    )
    # PostgreSQL exécute le DDL dans une transaction.
    # Sans commit, fermer cette connexion après un simple usage_count()
    # annulerait la création de la table.
    c.commit()


def _resolve_org_id(organization_id=None):
    if organization_id is not None:
        return organization_id

    org = current_org()
    return org["id"] if org else None


def usage_count(metric, organization_id=None, period=None):
    org_id = _resolve_org_id(organization_id)
    if not org_id:
        return 0

    period = period or _month_key()
    c = auth_cx()
    try:
        _ensure_usage_table(c)
        row = c.execute(
            "SELECT usage_count FROM plan_usage "
            "WHERE organization_id=? AND period=? AND metric=?",
            (org_id, period, metric),
        ).fetchone()
        return int(row["usage_count"]) if row else 0
    finally:
        c.close()


def quota_state(metric, organization_id=None, plan=None, period=None):
    if organization_id is not None:
        org_id = organization_id
        org = None
    else:
        org = current_org()
        org_id = org["id"] if org else None

    plan_name = plan or (org["plan"] if org else "TRIAL")
    limit = plan_limit(plan_name, metric)
    used = usage_count(metric, organization_id=org_id, period=period)

    return {
        "plan": plan_name,
        "metric": metric,
        "used": used,
        "limit": limit,
        "remaining": None if limit is None else max(0, int(limit) - used),
        "allowed": limit is None or used < int(limit),
        "period": period or _month_key(),
    }


def quota_allowed(metric, organization_id=None, plan=None):
    return quota_state(
        metric,
        organization_id=organization_id,
        plan=plan,
    )["allowed"]


def record_usage(metric, organization_id=None, amount=1, period=None):
    org_id = _resolve_org_id(organization_id)

    if not org_id:
        raise RuntimeError("Organisation introuvable pour le compteur de quota.")

    amount = int(amount)
    if amount <= 0:
        return usage_count(
            metric,
            organization_id=org_id,
            period=period,
        )

    period = period or _month_key()
    c = auth_cx()
    try:
        _ensure_usage_table(c)

        c.execute(
            """
            INSERT INTO plan_usage(
                organization_id,
                period,
                metric,
                usage_count,
                updated_at
            )
            VALUES(?,?,?,?,?)
            ON CONFLICT(organization_id,period,metric)
            DO UPDATE SET
                usage_count=plan_usage.usage_count + excluded.usage_count,
                updated_at=excluded.updated_at
            """,
            (
                org_id,
                period,
                metric,
                amount,
                now(),
            ),
        )
        c.commit()

        row = c.execute(
            "SELECT usage_count FROM plan_usage "
            "WHERE organization_id=? AND period=? AND metric=?",
            (org_id, period, metric),
        ).fetchone()

        return int(row["usage_count"]) if row else amount
    finally:
        c.close()

"""Exports RGPD (droit d'accès / portabilité) : archive ZIP de fichiers CSV
(séparateur « ; », UTF-8 avec BOM pour Excel).

- ``build_organization_export(org_id)`` : TOUTES les tables du tenant (schéma
  PostgreSQL ``org_<id>`` ou base SQLite) + métadonnées AUTH de l'organisation.
- ``build_personal_export(user_id)`` : données personnelles de l'utilisateur
  (profil, adhésions, activité, événements de sécurité, appareils, sessions).

Les colonnes de secrets (jetons, mots de passe, identifiants d'intégration...)
sont masquées : un export ne doit jamais devenir une clé d'accès.
"""
import csv
import io
import json
import re
import zipfile
from datetime import datetime, timezone

_SECRET_COLUMN = re.compile(r'(token|secret|password_hash|credential|key_hash|recovery_codes|^password$)', re.I)
REDACTED = '[masqué]'


def _safe_cell(value):
    from profitos.runtime import spreadsheet_safe
    if value is None:
        return ''
    if isinstance(value, (bytes, bytearray, memoryview)):
        return '[binaire]'
    return spreadsheet_safe(str(value))


def _rows_to_csv(rows, columns=None):
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=';', quoting=csv.QUOTE_MINIMAL, lineterminator='\r\n')
    rows = [dict(r) for r in rows]
    if columns is None:
        columns = list(rows[0].keys()) if rows else []
    writer.writerow(columns)
    for r in rows:
        writer.writerow([REDACTED if _SECRET_COLUMN.search(col) and r.get(col) not in (None, '') else _safe_cell(r.get(col))
                         for col in columns])
    return '﻿' + buf.getvalue()


def _columns(conn, table):
    try:
        return [r['name'] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()]
    except Exception:
        return None


def tenant_tables(conn):
    from profitos import db as dbmod
    if dbmod.USE_POSTGRES:
        rows = conn.execute("SELECT table_name AS name FROM information_schema.tables "
                            "WHERE table_schema=current_schema() AND table_type='BASE TABLE' "
                            "ORDER BY table_name").fetchall()
    else:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                            "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return [r['name'] for r in rows if re.fullmatch(r'[A-Za-z0-9_]+', r['name'] or '')]


def _write_table(zf, conn, table, path, where='', params=()):
    cols = _columns(conn, table)
    rows = conn.execute(f'SELECT * FROM {table} {where}', params).fetchall()
    zf.writestr(path, _rows_to_csv(rows, cols or None))
    return len(rows)


_AUTH_ORG_TABLES = (
    ('organizations', 'id'), ('memberships', 'organization_id'), ('activity_log', 'organization_id'),
    ('security_events', 'organization_id'), ('api_keys', 'organization_id'), ('api_audit_log', 'organization_id'),
    ('cabinet_time_entries', 'organization_id'), ('cabinet_documents', 'organization_id'),
    ('integration_interest', 'organization_id'), ('partner_directory', 'organization_id'),
    ('team_invitations', 'organization_id'), ('referrals', 'referrer_org_id'),
)


def build_organization_export(org_id):
    from profitos.runtime import auth_cx, tenant_cx_direct
    out = io.BytesIO()
    summary = {'organisation': int(org_id), 'genere_le': datetime.now(timezone.utc).isoformat(), 'tables': {}}
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:
        tc = tenant_cx_direct(org_id)
        try:
            for table in tenant_tables(tc):
                summary['tables'][table] = _write_table(zf, tc, table, f'organisation/{table}.csv')
        finally:
            tc.close()
        ac = auth_cx()
        try:
            for table, column in _AUTH_ORG_TABLES:
                try:
                    summary['tables'][f'compte/{table}'] = _write_table(
                        zf, ac, table, f'compte/{table}.csv', f'WHERE {column}=?', (int(org_id),))
                except Exception:
                    ac.rollback()
            members = ac.execute('SELECT u.id,u.email,u.full_name,u.created_at,m.role FROM memberships m '
                                 'JOIN users u ON u.id=m.user_id WHERE m.organization_id=?', (int(org_id),)).fetchall()
            zf.writestr('compte/membres.csv', _rows_to_csv(members, ['id', 'email', 'full_name', 'created_at', 'role']))
        finally:
            ac.close()
        zf.writestr('LISEZMOI.txt',
                    "Export RGPD ProfitOS — données de l'organisation.\r\n"
                    "Chaque fichier CSV correspond à une table (séparateur « ; », encodage UTF-8).\r\n"
                    "Les jetons, mots de passe et identifiants d'intégration sont masqués.\r\n")
        zf.writestr('sommaire.json', json.dumps(summary, ensure_ascii=False, indent=2))
    return out.getvalue()


_PROFILE_COLUMNS = ['id', 'email', 'full_name', 'is_active', 'email_verified', 'theme_preference', 'ui_mode',
                    'created_at', 'updated_at', 'password_changed_at', 'totp_enabled']


def build_personal_export(user_id):
    from profitos.runtime import auth_cx
    out = io.BytesIO()
    ac = auth_cx()
    try:
        user = ac.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
        profile = {k: (dict(user).get(k) if user else None) for k in _PROFILE_COLUMNS}
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('profil.csv', _rows_to_csv([profile], _PROFILE_COLUMNS))
            zf.writestr('profil.json', json.dumps(profile, ensure_ascii=False, indent=2, default=str))
            memberships = ac.execute('SELECT m.organization_id,o.name AS organisation,m.role,m.created_at FROM memberships m '
                                     'JOIN organizations o ON o.id=m.organization_id WHERE m.user_id=?',
                                     (user_id,)).fetchall()
            zf.writestr('adhesions.csv', _rows_to_csv(memberships, ['organization_id', 'organisation', 'role', 'created_at']))
            for table, name, cols in (
                ('activity_log', 'activite.csv', ['organization_id', 'event_type', 'description', 'created_at']),
                ('security_events', 'evenements_securite.csv', ['organization_id', 'event_type', 'outcome', 'target', 'user_agent', 'created_at']),
                ('user_devices', 'appareils.csv', ['first_seen_at', 'last_seen_at']),
                ('user_sessions', 'sessions.csv', ['user_agent', 'created_at', 'last_seen_at', 'revoked_at']),
                ('push_device_tokens', 'notifications_mobiles.csv', ['platform', 'created_at', 'last_seen_at']),
                ('cabinet_time_entries', 'temps_saisis.csv', ['organization_id', 'entry_date', 'duration_minutes', 'description', 'billable', 'created_at']),
            ):
                rows = ac.execute(f"SELECT {','.join(cols)} FROM {table} WHERE user_id=?", (user_id,)).fetchall()
                zf.writestr(name, _rows_to_csv(rows, cols))
            zf.writestr('LISEZMOI.txt',
                        "Export de vos données personnelles ProfitOS (art. 15 et 20 RGPD).\r\n"
                        "Les données comptables des organisations sont exportables par leur propriétaire.\r\n")
    finally:
        ac.close()
    return out.getvalue()

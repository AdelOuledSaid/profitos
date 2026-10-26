"""Moteur de vérification commun (v480).

Socle de Mission Control, et demain de Contract Guardian, Once & Done et Opportunity
Radar. Il ne génère aucune réponse : il assemble des constats tirés des données de
l'entreprise et de sources officielles, les vérifie, et n'autorise une action que si
toutes les vérifications passent. Règles :

1. Un constat porte ses preuves : chaque valeur a sa source et sa date.
2. Chaque constat est accompagné de contrôles explicites, à trois issues :
   vérifié (True), à vérifier (None : information manquante ou incertaine),
   impossible (False : un prérequis manque ou les informations se contredisent).
3. Une action n'est proposée que si tous les contrôles sont vérifiés ; avant de
   l'exécuter, le constat est recalculé (jamais d'après la page affichée).
4. Après l'action, le résultat est contrôlé : le problème doit disparaître des données
   avant la date de contrôle, sinon il est rouvert et signalé.
5. Un contrôle qui ne peut pas être fait le dit (« contrôle indisponible ») : jamais de
   silence, jamais de chiffre inventé.
"""
import json
from datetime import date, timedelta

READY, TO_CHECK, BLOCKED = 'pret', 'a_verifier', 'bloque'
LEVEL_LABELS = {READY: 'Prêt à traiter', TO_CHECK: "À vérifier avant d'agir", BLOCKED: "Action impossible pour l'instant"}
SEVERITIES = ('critique', 'important', 'info')
SEVERITY_LABELS = {'critique': 'Critique', 'important': 'Important', 'info': 'Information'}

# Cycle de vie d'un constat suivi.
OPEN, IN_CONTROL, RESOLVED, DISMISSED, SNOOZED = 'ouvert', 'en_controle', 'resolu', 'ecarte', 'reporte'
STATUS_LABELS = {OPEN: 'Ouvert', IN_CONTROL: 'Action faite, résultat en contrôle', RESOLVED: 'Résolu',
                 DISMISSED: 'Écarté', SNOOZED: 'Reporté'}


def check(label, ok, detail='', source=None):
    """Un contrôle : ok True (vérifié), None (à vérifier), False (impossible)."""
    if ok not in (True, False, None):
        ok = bool(ok)
    return {'label': label, 'ok': ok, 'detail': detail or '', 'source': source}


def evidence(label, value, source, as_of=None, url=None):
    """Une preuve : valeur, source et date de la donnée."""
    return {'label': label, 'value': value, 'source': source, 'as_of': as_of, 'url': url}


def level(checks):
    if any(c['ok'] is False for c in checks):
        return BLOCKED
    if any(c['ok'] is None for c in checks):
        return TO_CHECK
    return READY


def finding(key, module, kind, severity, title, *, checks, evidence=(), amount=None, detail='', action=None,
            alternatives=(), follow_up_days=7, ref=None):
    """Constat vérifiable. action : {'kind': 'execute'|'link', 'label', 'op'?, 'url'?, 'area'?, …}.
    alternatives : liens utiles quand l'action principale n'est pas encore possible."""
    if severity not in SEVERITIES:
        raise ValueError(severity)
    lv = level(checks)
    return {'key': key, 'module': module, 'kind': kind, 'severity': severity, 'title': title, 'detail': detail,
            'amount': None if amount is None else round(float(amount), 2), 'checks': list(checks),
            'evidence': list(evidence), 'level': lv, 'action': action, 'alternatives': list(alternatives),
            'action_allowed': bool(action) and lv == READY, 'follow_up_days': int(follow_up_days), 'ref': ref or {}}


def unavailable(module, name, exc):
    """Constat signalant qu'un contrôle n'a pas pu être fait (au lieu de l'ignorer)."""
    return finding(f'{module}:indisponible:{name}', module, 'controle_indisponible', 'info',
                   f'Contrôle « {name} » indisponible',
                   checks=[check('Contrôle exécuté', None, f'{type(exc).__name__} : le contrôle sera refait au prochain passage.')],
                   detail="Ce point n'a pas pu être vérifié : il n'est ni confirmé ni écarté.")


def can_execute(f):
    return bool(f) and f['action_allowed'] and (f.get('action') or {}).get('kind') == 'execute'


def sort_key(f):
    return (SEVERITIES.index(f['severity']), {READY: 0, TO_CHECK: 1, BLOCKED: 2}[f['level']], -(f['amount'] or 0), f['key'])


# ------------------------------------------------------------------ suivi des constats
def ensure_table(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS verification_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id INTEGER, item_key TEXT NOT NULL, module TEXT, kind TEXT,
        title TEXT, severity TEXT, amount REAL, status TEXT NOT NULL DEFAULT 'ouvert',
        first_seen TEXT, last_seen TEXT, action_label TEXT, action_at TEXT, action_by TEXT, action_result TEXT,
        check_after TEXT, resolved_at TEXT, resolution TEXT, snoozed_until TEXT, note TEXT, reopen_count INTEGER DEFAULT 0,
        ref TEXT, UNIQUE(entity_id, item_key))''')
    conn.commit()


def _get(conn, entity_id, key):
    return conn.execute('SELECT * FROM verification_items WHERE entity_id IS ? AND item_key=?', (entity_id, key)).fetchone()


def items(conn, entity_id, statuses=None):
    ensure_table(conn)
    if statuses:
        marks = ','.join('?' * len(statuses))
        return [dict(r) for r in conn.execute(
            f'SELECT * FROM verification_items WHERE entity_id IS ? AND status IN ({marks}) ORDER BY id',
            (entity_id, *statuses)).fetchall()]
    return [dict(r) for r in conn.execute('SELECT * FROM verification_items WHERE entity_id IS ? ORDER BY id',
                                          (entity_id,)).fetchall()]


def sync(conn, entity_id, findings, today, resolve_note=None, evaluated=None):
    """Rapproche les constats du jour du suivi enregistré et contrôle les résultats.

    - nouveau constat : enregistré « ouvert » ;
    - constat écarté : reste masqué ; reporté : masqué jusqu'à la date choisie ;
    - action faite : tant que la date de contrôle n'est pas passée, le constat reste « en
      contrôle » ; s'il est encore présent après, il est rouvert (« toujours présent ») ;
    - constat ouvert ou en contrôle qui a disparu des données : « résolu », avec la note
      fournie par resolve_note(item) (par ex. « facture payée le … »).
    evaluated : types de constats ('kind') réellement contrôlés ce jour (None = tous) ; un
    constat d'un type non contrôlé (contrôle en erreur, ou impossible hors session) n'est
    jamais soldé par défaut d'information.
    Retourne les constats visibles, enrichis de leur suivi ('track')."""
    ensure_table(conn)
    today_s = today.isoformat()
    seen, visible = set(), []
    for f in findings:
        seen.add(f['key'])
        row = _get(conn, entity_id, f['key'])
        ref = json.dumps(f.get('ref') or {}, sort_keys=True, default=str)
        if not row:
            conn.execute("""INSERT INTO verification_items(entity_id,item_key,module,kind,title,severity,amount,status,
                            first_seen,last_seen,ref) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                         (entity_id, f['key'], f['module'], f['kind'], f['title'], f['severity'], f['amount'], OPEN,
                          today_s, today_s, ref))
        else:
            status, note, reopen = row['status'], row['note'], int(row['reopen_count'] or 0)
            if status == RESOLVED:
                status, note, reopen = OPEN, f'Réapparu le {_fr(today)} après avoir été résolu.', reopen + 1
            elif status == SNOOZED and (row['snoozed_until'] or '') <= today_s:
                status, note = OPEN, None
            elif status == IN_CONTROL and row['check_after'] and row['check_after'] <= today_s:
                status, reopen = OPEN, reopen + 1
                note = (f"Toujours présent au contrôle du {_fr(today)} malgré l'action du "
                        f"{_fr(row['action_at'])} ({row['action_result'] or row['action_label'] or 'action'}).")
            conn.execute("""UPDATE verification_items SET title=?, severity=?, amount=?, last_seen=?, status=?, note=?,
                            reopen_count=?, ref=? WHERE id=?""",
                         (f['title'], f['severity'], f['amount'], today_s, status, note, reopen, ref, row['id']))
        row = dict(_get(conn, entity_id, f['key']))
        if row['status'] in (DISMISSED, SNOOZED):
            continue
        visible.append(dict(f, track=row))
    for row in conn.execute("SELECT * FROM verification_items WHERE entity_id IS ? AND status IN (?,?)",
                            (entity_id, OPEN, IN_CONTROL)).fetchall():
        if row['item_key'] in seen or (evaluated is not None and row['kind'] not in evaluated):
            continue
        item = dict(row)
        try:
            item['ref'] = json.loads(item.get('ref') or '{}')
        except ValueError:
            item['ref'] = {}
        note = None
        if resolve_note:
            try:
                note = resolve_note(item)
            except Exception:
                note = None
        note = note or f"Contrôlé le {_fr(today)} : le problème n'apparaît plus dans les données."
        conn.execute('UPDATE verification_items SET status=?, resolved_at=?, resolution=? WHERE id=?',
                     (RESOLVED, today_s, note, row['id']))
    conn.commit()
    return sorted(visible, key=sort_key)


def record_action(conn, entity_id, key, label, result, user, today, follow_up_days):
    ensure_table(conn)
    conn.execute("""UPDATE verification_items SET status=?, action_label=?, action_at=?, action_by=?, action_result=?,
                    check_after=?, note=NULL WHERE entity_id IS ? AND item_key=?""",
                 (IN_CONTROL, label, today.isoformat(), user, result, (today + timedelta(days=follow_up_days)).isoformat(),
                  entity_id, key))
    conn.commit()


def dismiss(conn, entity_id, key, reason, user, today):
    ensure_table(conn)
    conn.execute("""UPDATE verification_items SET status=?, note=?, action_by=?, resolved_at=? WHERE entity_id IS ? AND item_key=?""",
                 (DISMISSED, f'Écarté par {user} le {_fr(today)} : {reason}', user, today.isoformat(), entity_id, key))
    conn.commit()


def snooze(conn, entity_id, key, days, user, today):
    ensure_table(conn)
    until = today + timedelta(days=days)
    conn.execute("""UPDATE verification_items SET status=?, snoozed_until=?, note=? WHERE entity_id IS ? AND item_key=?""",
                 (SNOOZED, until.isoformat(), f'Reporté par {user} jusqu\'au {_fr(until)}.', entity_id, key))
    conn.commit()


def restore(conn, entity_id, key):
    ensure_table(conn)
    conn.execute("UPDATE verification_items SET status=?, note=NULL, snoozed_until=NULL WHERE entity_id IS ? AND item_key=?",
                 (OPEN, entity_id, key))
    conn.commit()


def _fr(value):
    if not value:
        return '—'
    d = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    return d.strftime('%d/%m/%Y')

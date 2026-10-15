"""Comptabilité complète — lot 1 : exercices comptables, grand livre, balances
(générale, clients, fournisseurs), contrepassation d'écriture et reprise d'un FEC.

Toutes les écritures passent par accounting.create_entry() : partie double,
période clôturée, numérotation et idempotence restent garanties au même endroit.
"""
import hashlib
import io
import re
from collections import OrderedDict
from datetime import date, datetime, timedelta

from profitos.accounting import AccountingError, create_entry, last_opening_date
from profitos.accounting import paris_today

MAX_FISCAL_YEAR_DAYS = 731  # premier exercice : 24 mois au plus


# ---------------------------------------------------------------------------
# Exercices comptables
# ---------------------------------------------------------------------------
def _entity_key(entity_id):
    return int(entity_id) if entity_id is not None else 0


def _d(value):
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def list_fiscal_years(conn, entity_id=None):
    rows = conn.execute(
        'SELECT * FROM accounting_fiscal_years WHERE entity_key=? ORDER BY start_date DESC',
        (_entity_key(entity_id),),
    ).fetchall()
    return [dict(r) for r in rows]


def add_fiscal_year(conn, start, end, entity_id=None):
    """Déclare un exercice. Contrôles : fin après début, 24 mois au plus, aucun
    chevauchement avec un exercice existant."""
    try:
        s, e = _d(start), _d(end)
    except (TypeError, ValueError):
        raise AccountingError("Dates d'exercice invalides.")
    if e <= s:
        raise AccountingError("La date de clôture doit être postérieure à la date d'ouverture.")
    if (e - s).days > MAX_FISCAL_YEAR_DAYS:
        raise AccountingError("Un exercice ne peut pas dépasser 24 mois.")
    for fy in list_fiscal_years(conn, entity_id):
        if s <= _d(fy['end_date']) and e >= _d(fy['start_date']):
            raise AccountingError(
                f"Cet exercice chevauche l'exercice du {_d(fy['start_date']):%d/%m/%Y} "
                f"au {_d(fy['end_date']):%d/%m/%Y}.")
    conn.execute(
        'INSERT INTO accounting_fiscal_years(entity_key,start_date,end_date,created_at) VALUES(?,?,?,?)',
        (_entity_key(entity_id), s.isoformat(), e.isoformat(), datetime.utcnow().isoformat()),
    )
    conn.commit()


def fiscal_year_for(conn, on_date=None, entity_id=None):
    """Exercice contenant on_date : celui déclaré, sinon déduit des exercices déclarés
    (même jour/mois de début), sinon l'année civile. Retourne
    {'start','end','declared','label'}."""
    day = _d(on_date) if on_date else paris_today()
    years = list_fiscal_years(conn, entity_id)
    for fy in years:
        if _d(fy['start_date']) <= day <= _d(fy['end_date']):
            s, e = _d(fy['start_date']), _d(fy['end_date'])
            return {'start': s, 'end': e, 'declared': True, 'label': _fy_label(s, e)}
    if years:
        ref = _d(years[0]['end_date']) + timedelta(days=1)  # exercices suivants : même rythme
        month, dom = ref.month, ref.day
    else:
        month, dom = 1, 1
    try:
        start = date(day.year, month, dom)
    except ValueError:
        start = date(day.year, month, 28)
    if start > day:
        start = date(start.year - 1, start.month, start.day)
    try:
        end = date(start.year + 1, start.month, start.day) - timedelta(days=1)
    except ValueError:
        end = date(start.year + 1, start.month, 28) - timedelta(days=1)
    return {'start': start, 'end': end, 'declared': False, 'label': _fy_label(start, end)}


def _fy_label(s, e):
    return f"{s:%d/%m/%Y} → {e:%d/%m/%Y}"


# ---------------------------------------------------------------------------
# Grand livre et balances
# ---------------------------------------------------------------------------
def _entity_clause(entity_id, alias='e'):
    if entity_id:
        return f'{alias}.entity_id=?', (entity_id,)
    return f'{alias}.entity_id IS NULL', ()


def _opening_movements(conn, date_from, entity_id, account_filter_sql='', account_params=()):
    """Mouvements antérieurs à date_from, par compte et tiers : comptes de bilan depuis
    les derniers à-nouveaux (ou depuis l'origine), comptes de gestion depuis le début
    de l'exercice. Clé : (compte, tiers)."""
    ef, ep = _entity_clause(entity_id)
    fy = fiscal_year_for(conn, date_from, entity_id)
    # À-nouveaux datés du premier jour de la période inclus : le report des comptes de
    # bilan repart alors de zéro et les à-nouveaux apparaissent comme mouvements.
    opening = last_opening_date(conn, _d(date_from).isoformat(), entity_id)
    bs_since = opening or '0000-01-01'
    rows = conn.execute(
        f"""SELECT l.account_code, COALESCE(l.auxiliary_name,'') aux, a.account_class,
               SUM(CASE WHEN a.account_class IN (6,7) AND e.entry_date<? THEN 0
                        WHEN a.account_class NOT IN (6,7) AND e.entry_date<? THEN 0
                        ELSE l.debit END) d,
               SUM(CASE WHEN a.account_class IN (6,7) AND e.entry_date<? THEN 0
                        WHEN a.account_class NOT IN (6,7) AND e.entry_date<? THEN 0
                        ELSE l.credit END) c
            FROM accounting_entry_lines l
            JOIN accounting_entries e ON e.id=l.entry_id
            JOIN accounting_chart_of_accounts a ON a.code=l.account_code
            WHERE e.entry_date<? AND {ef} {account_filter_sql}
            GROUP BY l.account_code, COALESCE(l.auxiliary_name,''), a.account_class""",
        (fy['start'].isoformat(), bs_since, fy['start'].isoformat(), bs_since,
         _d(date_from).isoformat(), *ep, *account_params),
    ).fetchall()
    return {(r['account_code'], r['aux']): (round(r['d'] or 0, 2), round(r['c'] or 0, 2)) for r in rows}


def _account_filter(account_from=None, account_to=None, prefix=None):
    sql, params = '', []
    if account_from:
        sql += ' AND l.account_code>=?'; params.append(account_from)
    if account_to:
        # « au compte 411 » inclut 411000, 41100001… (borne sûre quelle que soit la collation)
        sql += ' AND l.account_code<=?'; params.append(account_to.ljust(12, 'z'))
    if prefix:
        sql += ' AND l.account_code LIKE ?'; params.append(prefix + '%')
    return sql, tuple(params)


def general_ledger(conn, date_from, date_to, entity_id=None, account_from=None, account_to=None, auxiliary=None):
    """Grand livre : par compte, report (solde antérieur à la période), mouvements
    avec solde progressif, totaux. Le report est présenté sur une ligne unique."""
    ef, ep = _entity_clause(entity_id)
    fsql, fparams = _account_filter(account_from, account_to)
    if auxiliary:
        fsql += ' AND l.auxiliary_name=?'
        fparams += (auxiliary,)
    openings = _opening_movements(conn, date_from, entity_id, fsql, fparams)
    rows = conn.execute(
        f"""SELECT l.id line_id, l.account_code, a.label account_label, l.auxiliary_name, l.label,
               l.debit, l.credit, l.lettrage_code, e.id entry_id, e.entry_date, e.journal_code,
               e.piece_number, e.piece_ref, e.label entry_label
            FROM accounting_entry_lines l
            JOIN accounting_entries e ON e.id=l.entry_id
            JOIN accounting_chart_of_accounts a ON a.code=l.account_code
            WHERE e.entry_date BETWEEN ? AND ? AND {ef} {fsql}
            ORDER BY l.account_code, e.entry_date, e.id, l.line_order""",
        (_d(date_from).isoformat(), _d(date_to).isoformat(), *ep, *fparams),
    ).fetchall()
    labels = {r['code']: r['label'] for r in conn.execute('SELECT code,label FROM accounting_chart_of_accounts').fetchall()}
    accounts = OrderedDict()
    report_by_account = {}
    for (code, _aux), (d, c) in openings.items():
        report_by_account[code] = round(report_by_account.get(code, 0) + d - c, 2)
    for code in sorted(set(report_by_account) | {r['account_code'] for r in rows}):
        rep = report_by_account.get(code, 0.0)
        accounts[code] = {'code': code, 'label': labels.get(code, ''), 'report': rep, 'lines': [],
                          'total_debit': 0.0, 'total_credit': 0.0, 'balance': rep}
    for r in rows:
        acc = accounts[r['account_code']]
        acc['total_debit'] = round(acc['total_debit'] + (r['debit'] or 0), 2)
        acc['total_credit'] = round(acc['total_credit'] + (r['credit'] or 0), 2)
        acc['balance'] = round(acc['balance'] + (r['debit'] or 0) - (r['credit'] or 0), 2)
        line = dict(r)
        line['running'] = acc['balance']
        acc['lines'].append(line)
    out = [a for a in accounts.values() if a['lines'] or abs(a['report']) >= 0.005]
    return {
        'accounts': out,
        'total_debit': round(sum(a['total_debit'] for a in out), 2),
        'total_credit': round(sum(a['total_credit'] for a in out), 2),
    }


def trial_balance(conn, date_from, date_to, entity_id=None, kind='generale'):
    """Balance générale (par compte) ou auxiliaire (clients 411 / fournisseurs 401, par
    tiers) : solde d'ouverture, mouvements de la période, solde de clôture."""
    ef, ep = _entity_clause(entity_id)
    prefix = {'clients': '411', 'fournisseurs': '401'}.get(kind)
    fsql, fparams = _account_filter(prefix=prefix)
    by_aux = prefix is not None
    openings = _opening_movements(conn, date_from, entity_id, fsql, fparams)
    rows = conn.execute(
        f"""SELECT l.account_code, COALESCE(l.auxiliary_name,'') aux,
               COALESCE(SUM(l.debit),0) d, COALESCE(SUM(l.credit),0) c
            FROM accounting_entry_lines l
            JOIN accounting_entries e ON e.id=l.entry_id
            WHERE e.entry_date BETWEEN ? AND ? AND {ef} {fsql}
            GROUP BY l.account_code, COALESCE(l.auxiliary_name,'')""",
        (_d(date_from).isoformat(), _d(date_to).isoformat(), *ep, *fparams),
    ).fetchall()
    labels = {r['code']: r['label'] for r in conn.execute('SELECT code,label FROM accounting_chart_of_accounts').fetchall()}
    agg = {}

    def key(code, aux):
        return (aux or '(sans tiers)') if by_aux else code
    for (code, aux), (d, c) in openings.items():
        k = key(code, aux)
        a = agg.setdefault(k, {'opening': 0.0, 'debit': 0.0, 'credit': 0.0, 'code': code})
        a['opening'] = round(a['opening'] + d - c, 2)
    for r in rows:
        k = key(r['account_code'], r['aux'])
        a = agg.setdefault(k, {'opening': 0.0, 'debit': 0.0, 'credit': 0.0, 'code': r['account_code']})
        a['debit'] = round(a['debit'] + r['d'], 2)
        a['credit'] = round(a['credit'] + r['c'], 2)
    lines = []
    for k in sorted(agg):
        a = agg[k]
        closing = round(a['opening'] + a['debit'] - a['credit'], 2)
        if not (abs(a['opening']) >= 0.005 or a['debit'] or a['credit']):
            continue
        lines.append({
            'key': k,
            'label': k if by_aux else labels.get(k, ''),
            'account_code': a['code'],
            'opening_debit': a['opening'] if a['opening'] > 0 else 0.0,
            'opening_credit': -a['opening'] if a['opening'] < 0 else 0.0,
            'debit': a['debit'], 'credit': a['credit'],
            'closing_debit': closing if closing > 0 else 0.0,
            'closing_credit': -closing if closing < 0 else 0.0,
        })
    totals = {f: round(sum(l[f] for l in lines), 2) for f in
              ('opening_debit', 'opening_credit', 'debit', 'credit', 'closing_debit', 'closing_credit')}
    totals['balanced'] = abs(totals['debit'] - totals['credit']) < 0.01 if kind == 'generale' else None
    return {'lines': lines, 'totals': totals, 'kind': kind}


# ---------------------------------------------------------------------------
# Contrepassation
# ---------------------------------------------------------------------------
def reverse_entry(conn, entry_id, reversal_date, created_by=None, entity_id=None):
    """Contrepasse une écriture (débits et crédits inversés) à la date choisie. Une
    écriture ne peut être contrepassée qu'une fois ; une contrepassation ne se
    contrepasse pas."""
    ef, ep = _entity_clause(entity_id, alias='accounting_entries')
    entry = conn.execute(f'SELECT * FROM accounting_entries WHERE id=? AND {ef}', (entry_id, *ep)).fetchone()
    if not entry:
        raise AccountingError("Écriture introuvable.")
    if entry['source_type'] == 'reversal':
        raise AccountingError("Une contrepassation ne peut pas elle-même être contrepassée.")
    lines = conn.execute('SELECT * FROM accounting_entry_lines WHERE entry_id=? ORDER BY line_order',
                         (entry_id,)).fetchall()
    reversed_lines = [{'account_code': l['account_code'], 'auxiliary_name': l['auxiliary_name'],
                       'label': l['label'], 'debit': l['credit'], 'credit': l['debit']} for l in lines]
    try:
        return create_entry(conn, entry['journal_code'], _d(reversal_date),
                            f"Contrepassation {entry['piece_number']} — {entry['label']}"[:250],
                            reversed_lines, source_type='reversal', source_id=int(entry_id),
                            created_by=created_by, entity_id=entity_id, piece_ref=entry['piece_number'])
    except AccountingError as exc:
        if 'existe déjà' in str(exc) or 'déjà en cours' in str(exc):
            raise AccountingError("Cette écriture a déjà été contrepassée.")
        raise


# ---------------------------------------------------------------------------
# Reprise d'un FEC (Fichier des Écritures Comptables)
# ---------------------------------------------------------------------------
MAX_FEC_BYTES = 30 * 1024 * 1024
JOURNAL_TYPES = {'VE': 'VENTES', 'AC': 'ACHATS', 'HA': 'ACHATS', 'BQ': 'BANQUE', 'BA': 'BANQUE',
                 'CA': 'CAISSE', 'AN': 'AN', 'OD': 'OD'}


def _fec_number(value):
    t = (value or '').strip().replace(' ', '').replace(' ', '')
    if not t:
        return 0.0
    if ',' in t and '.' in t:
        t = t.replace('.', '').replace(',', '.') if t.rfind(',') > t.rfind('.') else t.replace(',', '')
    else:
        t = t.replace(',', '.')
    return round(float(t), 2)


def _fec_date(value):
    t = (value or '').strip()
    for fmt in ('%Y%m%d', '%Y-%m-%d', '%d/%m/%Y'):
        try:
            return datetime.strptime(t[:10], fmt).date()
        except ValueError:
            continue
    raise ValueError(f"date illisible « {t} »")


def parse_fec(data):
    """Lit un FEC (séparateur | ou tabulation, UTF-8 ou ISO-8859-15) et regroupe les
    lignes par écriture. Gère les colonnes Debit/Credit ou Montant/Sens. Retourne
    {'entries': [...], 'accounts': {code: label}, 'journals': {code: label},
     'date_from', 'date_to', 'lines'} ou lève ValueError avec un message clair."""
    if len(data) > MAX_FEC_BYTES:
        raise ValueError("Fichier trop volumineux (30 Mo au plus).")
    for enc in ('utf-8-sig', 'iso-8859-15'):
        try:
            text = data.decode(enc); break
        except UnicodeDecodeError:
            continue
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise ValueError("Fichier vide ou sans écritures.")
    sep = '|' if lines[0].count('|') >= lines[0].count('\t') else '\t'
    header = [h.strip().lower() for h in lines[0].split(sep)]
    idx = {h: i for i, h in enumerate(header)}
    required = ['journalcode', 'ecriturenum', 'ecrituredate', 'comptenum', 'ecriturelib']
    missing = [r for r in required if r not in idx]
    if missing or not (('debit' in idx and 'credit' in idx) or ('montant' in idx and 'sens' in idx)):
        raise ValueError("Ce fichier n'a pas l'en-tête d'un FEC (colonnes JournalCode, EcritureNum, "
                         "EcritureDate, CompteNum, EcritureLib, Debit, Credit…).")

    def col(parts, name, default=''):
        i = idx.get(name)
        return parts[i].strip() if i is not None and i < len(parts) else default

    entries = OrderedDict()
    accounts, journals = {}, {}
    dmin = dmax = None
    for n, raw in enumerate(lines[1:], start=2):
        parts = raw.split(sep)
        try:
            jcode = col(parts, 'journalcode')[:10]
            num = col(parts, 'ecriturenum')
            edate = _fec_date(col(parts, 'ecrituredate'))
            account = col(parts, 'comptenum')
            if not (jcode and num and account):
                raise ValueError("journal, numéro d'écriture ou compte manquant")
            if not account[0].isdigit() or account[0] not in '12345678':
                raise ValueError(f"compte « {account} » hors plan comptable (classe 1 à 8)")
            if 'debit' in idx:
                debit, credit = _fec_number(col(parts, 'debit')), _fec_number(col(parts, 'credit'))
            else:
                amount = _fec_number(col(parts, 'montant'))
                sens = col(parts, 'sens').upper()[:1]
                debit, credit = (amount, 0.0) if sens == 'D' else (0.0, amount)
        except ValueError as exc:
            raise ValueError(f"Ligne {n} : {exc}.")
        if debit < 0 or credit < 0:  # montant négatif : on le passe dans l'autre colonne
            debit, credit = (abs(credit), abs(debit))
        if not debit and not credit:
            continue
        accounts.setdefault(account, col(parts, 'comptelib') or account)
        journals.setdefault(jcode, col(parts, 'journallib') or jcode)
        aux = col(parts, 'compauxlib') or col(parts, 'compauxnum') or None
        key = (jcode, num)
        e = entries.setdefault(key, {'journal': jcode, 'num': num, 'date': edate,
                                     'label': col(parts, 'ecriturelib') or num,
                                     'piece_ref': col(parts, 'pieceref') or num, 'lines': []})
        e['lines'].append({'account_code': account, 'auxiliary_name': aux,
                           'label': col(parts, 'ecriturelib') or e['label'],
                           'debit': debit, 'credit': credit})
        dmin = edate if dmin is None or edate < dmin else dmin
        dmax = edate if dmax is None or edate > dmax else dmax
    unbalanced = [f"{k[0]} {k[1]}" for k, e in entries.items()
                  if abs(sum(l['debit'] for l in e['lines']) - sum(l['credit'] for l in e['lines'])) > 0.01]
    if unbalanced:
        raise ValueError("Écritures déséquilibrées dans le fichier : " + ', '.join(unbalanced[:5])
                         + (f" (+{len(unbalanced) - 5})" if len(unbalanced) > 5 else '') + '.')
    if not entries:
        raise ValueError("Aucune écriture avec un montant dans ce fichier.")
    return {'entries': list(entries.values()), 'accounts': accounts, 'journals': journals,
            'date_from': dmin, 'date_to': dmax, 'lines': sum(len(e['lines']) for e in entries.values())}


def _ensure_accounts_and_journals(conn, parsed):
    now = datetime.utcnow().isoformat()
    created_accounts = 0
    for code, label in parsed['accounts'].items():
        cur = conn.execute(
            'INSERT OR IGNORE INTO accounting_chart_of_accounts'
            '(code,label,account_class,is_collective,is_active,is_default,created_at) VALUES(?,?,?,?,1,0,?)',
            (code, label[:200], int(code[0]), 1 if code[:3] in ('401', '411') else 0, now))
        created_accounts += cur.rowcount or 0
    for code, label in parsed['journals'].items():
        conn.execute(
            'INSERT OR IGNORE INTO accounting_journals(code,label,journal_type,is_default,created_at) VALUES(?,?,?,0,?)',
            (code, label[:120], JOURNAL_TYPES.get(code.upper()[:2], 'OD'), now))
    return created_accounts


def import_fec(conn, data, filename, mode='ecritures', entity_id=None, created_by=None, opening_date=None):
    """Importe un FEC. mode='ecritures' : toutes les écritures, journal par journal ;
    mode='soldes' : une seule écriture d'à-nouveaux (journal AN) reprenant les soldes des
    comptes de bilan au dernier jour du fichier, résultat porté en 120000 / 129000.
    Tout ou rien : la moindre erreur annule l'import. Un même fichier n'est importé
    qu'une fois."""
    sha = hashlib.sha256(data).hexdigest()
    ek = _entity_key(entity_id)
    if conn.execute('SELECT 1 FROM accounting_import_batches WHERE entity_key=? AND file_sha256=?',
                    (ek, sha)).fetchone():
        raise AccountingError("Ce fichier a déjà été importé.")
    try:
        parsed = parse_fec(data)
    except ValueError as exc:
        raise AccountingError(str(exc))
    try:
        created_accounts = _ensure_accounts_and_journals(conn, parsed)
        count = 0
        if mode == 'soldes':
            balances = {}
            for e in parsed['entries']:
                for l in e['lines']:
                    if l['account_code'][0] in '12345':
                        k = (l['account_code'], l['auxiliary_name'] if l['account_code'][:3] in ('401', '411') else None)
                        balances[k] = round(balances.get(k, 0) + l['debit'] - l['credit'], 2)
            lines = []
            for (code, aux), bal in sorted(balances.items(), key=lambda kv: (kv[0][0], kv[0][1] or '')):
                if abs(bal) < 0.005:
                    continue
                lines.append({'account_code': code, 'auxiliary_name': aux, 'label': 'Solde repris',
                              'debit': bal if bal > 0 else 0, 'credit': -bal if bal < 0 else 0})
            residual = round(sum(l['debit'] - l['credit'] for l in lines), 2)
            if abs(residual) >= 0.005:  # résultat de l'exercice repris, en attente d'affectation
                lines.append({'account_code': '120000' if residual > 0 else '129000',
                              'label': "Résultat de l'exercice repris",
                              'debit': 0 if residual > 0 else -residual, 'credit': residual if residual > 0 else 0})
            day = _d(opening_date) if opening_date else parsed['date_to'] + timedelta(days=1)
            create_entry(conn, 'AN', day, f"Reprise des soldes au {day:%d/%m/%Y} ({filename})"[:250],
                         lines, created_by=created_by, entity_id=entity_id, commit=False,
                         piece_ref=f"FEC-{sha[:8]}")
            count = 1
        else:
            for e in parsed['entries']:
                create_entry(conn, e['journal'], e['date'], e['label'][:250], e['lines'],
                             created_by=created_by, entity_id=entity_id, commit=False,
                             piece_ref=e['piece_ref'][:120])
                count += 1
        conn.execute(
            'INSERT INTO accounting_import_batches(entity_key,file_sha256,filename,mode,entries_count,lines_count,'
            'imported_by,imported_at) VALUES(?,?,?,?,?,?,?,?)',
            (ek, sha, (filename or '')[:200], mode, count, parsed['lines'], created_by, datetime.utcnow().isoformat()))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {'entries': count, 'lines': parsed['lines'], 'accounts_created': created_accounts,
            'date_from': parsed['date_from'], 'date_to': parsed['date_to'], 'mode': mode}


# ---------------------------------------------------------------------------
# Exports tableur
# ---------------------------------------------------------------------------
def ledger_rows(gl):
    rows = []
    for a in gl['accounts']:
        rows.append({'Compte': a['code'], 'Libellé compte': a['label'], 'Date': '', 'Journal': '', 'Pièce': '',
                     'Libellé': 'Report', 'Débit': a['report'] if a['report'] > 0 else 0,
                     'Crédit': -a['report'] if a['report'] < 0 else 0, 'Solde': a['report'], 'Lettrage': ''})
        for l in a['lines']:
            rows.append({'Compte': a['code'], 'Libellé compte': a['label'], 'Date': l['entry_date'],
                         'Journal': l['journal_code'], 'Pièce': l['piece_number'],
                         'Libellé': l['label'] or l['entry_label'], 'Débit': l['debit'], 'Crédit': l['credit'],
                         'Solde': l['running'], 'Lettrage': l['lettrage_code'] or ''})
    return rows


def balance_rows(tb):
    first = 'Tiers' if tb['kind'] != 'generale' else 'Compte'
    return [{first: l['key'], 'Libellé': l['label'], 'Ouverture débit': l['opening_debit'],
             'Ouverture crédit': l['opening_credit'], 'Mouvements débit': l['debit'],
             'Mouvements crédit': l['credit'], 'Solde débiteur': l['closing_debit'],
             'Solde créditeur': l['closing_credit']} for l in tb['lines']]

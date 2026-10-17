"""Déclarations des sommes versées à des tiers :

- DAS2 (état des honoraires, commissions, courtages, droits d'auteur… article 240 du
  CGI) : sommes versées pendant l'année civile, TTC, à chaque bénéficiaire qui a reçu
  plus de 1 200 € ; déposée avec la liasse (au plus tard le deuxième jour ouvré suivant
  le 1er mai pour une clôture au 31 décembre) ou en janvier ;
- 2777 (revenus de capitaux mobiliers) : prélèvement forfaitaire non libératoire de
  12,8 % et prélèvements sociaux sur les dividendes versés aux associés personnes
  physiques, déclarés et payés en ligne au plus tard le 15 du mois suivant le versement.

Les montants sont tirés de la comptabilité et doivent être vérifiés : la DAS2 vise les
sommes réellement versées (et non comptabilisées), la 2777 les dividendes payés.
"""
import json
import re
from datetime import date, timedelta

DAS2_THRESHOLD = 1200.0
# Catégories de la DAS2 et comptes du plan comptable correspondants.
DAS2_CATEGORIES = {
    'HON': 'Honoraires et vacations',
    'COM': 'Commissions',
    'COU': 'Courtages',
    'RIS': 'Ristournes',
    'JPR': 'Jetons de présence',
    'DAU': "Droits d'auteur",
    'DIN': "Droits d'inventeur",
    'AUT': 'Autres rémunérations',
}
DAS2_ACCOUNTS = [('6226', 'HON'), ('6227', 'HON'), ('6221', 'COM'), ('6222', 'COM'), ('6516', 'DAU'),
                 ('6511', 'DIN'), ('653', 'JPR')]
SUPPLIER_ACCOUNTS = ('401', '404', '408', '467')

PFNL_RATE = 12.8


def social_levies_rate(on_date):
    """Prélèvements sociaux sur les revenus de capitaux mobiliers : 18,6 % depuis le
    1er janvier 2026 (hausse de la CSG), 17,2 % auparavant."""
    return 18.6 if on_date >= date(2026, 1, 1) else 17.2


def _r(x):
    return round(float(x or 0), 2)


def _ekey(entity_id):
    return int(entity_id) if entity_id else 0


def _ef(entity_id):
    return ('e.entity_id=?', (entity_id,)) if entity_id else ('e.entity_id IS NULL', ())


def _category(code):
    best, blen = None, 0
    for prefix, cat in DAS2_ACCOUNTS:
        if code.startswith(prefix) and len(prefix) > blen:
            best, blen = cat, len(prefix)
    return best


def _key(name):
    return re.sub(r'[^a-z0-9]+', ' ', (name or '').lower()).strip()


def load_store(conn, entity_id, key):
    row = conn.execute('SELECT data FROM fiscal_settings WHERE entity_key=? AND fy_end=?',
                       (_ekey(entity_id), key)).fetchone()
    if row and row['data']:
        try:
            return json.loads(row['data'])
        except ValueError:
            pass
    return {}


def save_store(conn, entity_id, key, data, now_iso, user=None):
    conn.execute(
        """INSERT INTO fiscal_settings(entity_key,fy_end,data,updated_at,updated_by) VALUES(?,?,?,?,?)
           ON CONFLICT(entity_key,fy_end) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at,
           updated_by=excluded.updated_by""",
        (_ekey(entity_id), key, json.dumps(data, ensure_ascii=False), now_iso, user))
    conn.commit()


# ------------------------------------------------------------------------- DAS2
def das2_deadline(year, fy_end_month_day=(12, 31)):
    """Avec la liasse pour une clôture au 31 décembre ; sinon le 31 janvier."""
    if tuple(fy_end_month_day) == (12, 31):
        from profitos.corporate_tax import second_working_day_after_may_1
        return second_working_day_after_may_1(year + 1)
    return date(year + 1, 1, 31)


def das2_candidates(conn, year, entity_id=None):
    """Bénéficiaires des sommes de l'année, à partir des écritures portant sur les comptes
    d'honoraires, commissions, droits d'auteur… Le montant TTC de chaque ligne est obtenu
    en appliquant le rapport TTC / HT de sa pièce."""
    ef, ep = _ef(entity_id)
    rows = conn.execute(
        f"""SELECT l.entry_id, l.account_code, l.auxiliary_name, l.debit, l.credit, e.label elabel, e.entry_date
            FROM accounting_entry_lines l JOIN accounting_entries e ON e.id=l.entry_id
            WHERE {ef} AND e.journal_code<>'AN' AND e.entry_date BETWEEN ? AND ?
              AND e.id IN (SELECT l2.entry_id FROM accounting_entry_lines l2 WHERE {' OR '.join('l2.account_code LIKE ?' for _ in DAS2_ACCOUNTS)})
            ORDER BY e.entry_date, l.entry_id, l.id""",
        (*ep, f'{year}-01-01', f'{year}-12-31', *[p + '%' for p, _ in DAS2_ACCOUNTS])).fetchall()
    entries = {}
    for r in rows:
        entries.setdefault(r['entry_id'], []).append(r)
    benef = {}
    for eid_, lines in entries.items():
        supplier = [l for l in lines if str(l['account_code']).startswith(SUPPLIER_ACCOUNTS)]
        name = next((l['auxiliary_name'] for l in supplier if l['auxiliary_name']), None) \
            or next((l['auxiliary_name'] for l in lines if l['auxiliary_name']), None) or lines[0]['elabel']
        ht_base = sum(float(l['debit'] or 0) - float(l['credit'] or 0) for l in lines
                      if str(l['account_code'])[0] in '62' and not str(l['account_code']).startswith('445'))
        ttc = sum(float(l['credit'] or 0) - float(l['debit'] or 0) for l in supplier) if supplier else \
            sum(float(l['credit'] or 0) - float(l['debit'] or 0) for l in lines if str(l['account_code'])[0] == '5')
        ratio = (ttc / ht_base) if ht_base > 0 and ttc > 0 else 1.0
        for l in lines:
            cat = _category(str(l['account_code']))
            if not cat:
                continue
            amount = (float(l['debit'] or 0) - float(l['credit'] or 0)) * ratio
            b = benef.setdefault(_key(name), {'key': _key(name), 'name': name, 'categories': {}, 'amount': 0.0,
                                              'entries': 0})
            b['categories'][cat] = _r(b['categories'].get(cat, 0) + amount)
            b['amount'] = _r(b['amount'] + amount)
            b['entries'] += 1
    return sorted(benef.values(), key=lambda b: -b['amount'])


def das2(conn, year, entity_id=None, fy_end_month_day=(12, 31)):
    """État DAS2 de l'année : bénéficiaires retenus (plus de 1 200 € TTC), complétés par les
    fiches fournisseurs et par les corrections enregistrées."""
    store = load_store(conn, entity_id, f'das2-{year}')
    saved = store.get('beneficiaries') or {}
    try:
        suppliers = {_key(s['name']): s for s in conn.execute('SELECT name,address,siret FROM suppliers').fetchall()}
    except Exception:
        suppliers = {}
    out = []
    for b in das2_candidates(conn, year, entity_id):
        s = saved.get(b['key']) or {}
        sup = suppliers.get(b['key'])
        amount = _r(s['amount']) if s.get('amount') not in (None, '') else b['amount']
        main_cat = max(b['categories'], key=lambda k: b['categories'][k]) if b['categories'] else 'HON'
        row = dict(b, amount_booked=b['amount'], amount=amount,
                   category=s.get('category') or main_cat,
                   siret=s.get('siret') or (sup['siret'] if sup and sup['siret'] else ''),
                   address=s.get('address') or (sup['address'] if sup and sup['address'] else ''),
                   profession=s.get('profession') or '',
                   above=amount > DAS2_THRESHOLD)
        row['include'] = bool(s['include']) if 'include' in s else row['above']
        out.append(row)
    included = [r for r in out if r['include']]
    issues = []
    for r in included:
        if not r['siret']:
            issues.append(f"{r['name']} : SIRET (ou numéro fiscal) manquant.")
        if not r['address']:
            issues.append(f"{r['name']} : adresse manquante.")
    return {'year': year, 'rows': out, 'included': included, 'total': _r(sum(r['amount'] for r in included)),
            'issues': issues, 'deadline': das2_deadline(year, fy_end_month_day), 'threshold': DAS2_THRESHOLD}


def save_das2(conn, entity_id, year, rows, now_iso, user=None):
    store = load_store(conn, entity_id, f'das2-{year}')
    store['beneficiaries'] = rows
    save_store(conn, entity_id, f'das2-{year}', store, now_iso, user)


# ------------------------------------------------------------------------- 2777
def rcm_deadline(month_start):
    nxt = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt.replace(day=15)


def rcm_2777(conn, month_start, entity_id=None, profile=None):
    """Dividendes payés dans le mois (débits du compte 457), part revenant aux associés
    personnes physiques (profil fiscal, ou part saisie), prélèvement de 12,8 % (hors
    associés dispensés) et prélèvements sociaux, comparés aux retenues comptabilisées au
    compte 442."""
    month_start = month_start.replace(day=1)
    month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    ef, ep = _ef(entity_id)

    def mv(prefix, side):
        return _r(conn.execute(
            f"""SELECT COALESCE(SUM(l.{side}),0) t FROM accounting_entry_lines l JOIN accounting_entries e ON e.id=l.entry_id
                WHERE {ef} AND e.journal_code<>'AN' AND e.entry_date BETWEEN ? AND ? AND l.account_code LIKE ?""",
            (*ep, month_start.isoformat(), month_end.isoformat(), prefix + '%')).fetchone()['t'])

    paid = mv('457', 'debit')
    withheld = _r(mv('442', 'credit') - mv('442', 'debit'))
    store = load_store(conn, entity_id, f"rcm-{month_start.strftime('%Y-%m')}")
    shareholders = [r for r in ((profile or {}).get('shareholders') or []) if (r.get('name') or '').strip()]
    pp_pct = sum(float(r.get('pct') or 0) for r in shareholders if r.get('kind') != 'PM')
    all_pct = sum(float(r.get('pct') or 0) for r in shareholders)
    warnings = []
    if store.get('part_pp') not in (None, ''):
        part_pp = float(store['part_pp'])
    elif all_pct > 0:
        part_pp = round(100 * pp_pct / all_pct, 2)
    else:
        part_pp = 100.0
        if paid:
            warnings.append("Composition du capital non renseignée : les dividendes sont supposés versés en totalité à des "
                            "personnes physiques. Complétez le profil fiscal ou saisissez la part.")
    gross_pp = _r(paid * part_pp / 100)
    dispensed = min(gross_pp, _r(store.get('dispensed') or 0))
    ps_rate = social_levies_rate(month_start)
    pfnl = _r((gross_pp - dispensed) * PFNL_RATE / 100)
    ps = _r(gross_pp * ps_rate / 100)
    total = _r(pfnl + ps)
    if paid and abs(withheld - total) >= 1:
        fr = lambda x: f"{x:,.2f}".replace(',', ' ').replace('.', ',')
        warnings.append(f"Les retenues comptabilisées au compte 442 ({fr(withheld)} €) diffèrent du montant calculé "
                        f"({fr(total)} €) : vérifiez l'écriture de paiement des dividendes.")
    return {'month': month_start, 'end': month_end, 'paid': paid, 'part_pp': part_pp, 'gross_pp': gross_pp,
            'dispensed': dispensed, 'pfnl_rate': PFNL_RATE, 'pfnl': pfnl, 'ps_rate': ps_rate, 'ps': ps, 'total': total,
            'withheld': withheld, 'deadline': rcm_deadline(month_start), 'warnings': warnings,
            'store': store}


def dividend_months(conn, entity_id=None, since=None):
    """Mois où des dividendes ont été payés (débits du 457)."""
    ef, ep = _ef(entity_id)
    since = since or date(2000, 1, 1)
    rows = conn.execute(
        f"""SELECT DISTINCT substr(e.entry_date,1,7) m FROM accounting_entry_lines l JOIN accounting_entries e ON e.id=l.entry_id
            WHERE {ef} AND e.journal_code<>'AN' AND l.account_code LIKE ? AND l.debit>0 AND e.entry_date>=? ORDER BY m DESC""",
        (*ep, '457%', since.isoformat())).fetchall()
    return [date.fromisoformat(r['m'] + '-01') for r in rows]


# ------------------------------------------------------------------------- IFU 2561
def ifu_deadline(year):
    return date(year + 1, 2, 15)


def ifu_2561(conn, year, entity_id=None, profile=None):
    """Imprimé fiscal unique : dividendes payés pendant l'année à chaque associé, avec les
    montants à reporter sur sa déclaration de revenus (2042 : 2DC revenus éligibles à
    l'abattement de 40 %, 2CK prélèvement forfaitaire non libératoire retenu, 2BH revenus
    soumis aux prélèvements sociaux avec CSG déductible). Les paiements sont attribués
    par le nom porté sur la ligne du compte 457, sinon au prorata du capital détenu."""
    ef, ep = _ef(entity_id)
    lines = conn.execute(
        f"""SELECT l.auxiliary_name, l.debit, e.entry_date FROM accounting_entry_lines l
            JOIN accounting_entries e ON e.id=l.entry_id
            WHERE {ef} AND e.journal_code<>'AN' AND l.account_code LIKE ? AND l.debit>0 AND e.entry_date BETWEEN ? AND ?
            ORDER BY e.entry_date""", (*ep, '457%', f'{year}-01-01', f'{year}-12-31')).fetchall()
    store = load_store(conn, entity_id, f'ifu-{year}')
    dispensed = set(store.get('dispensed') or [])
    holders = [r for r in ((profile or {}).get('shareholders') or []) if (r.get('name') or '').strip()]
    by_key = {}
    for h in holders:
        by_key[_key(h['name'])] = h
        if h.get('first_name'):
            by_key[_key(f"{h['first_name']} {h['name']}")] = h
            by_key[_key(f"{h['name']} {h['first_name']}")] = h
    total_pct = sum(float(h.get('pct') or 0) for h in holders)
    benef = {}

    def add(holder, amount, on):
        k = _key(f"{holder.get('first_name') or ''} {holder['name']}")
        b = benef.setdefault(k, {'key': k, 'name': ' '.join(x for x in (holder.get('first_name'), holder['name']) if x),
                                 'kind': holder.get('kind') or 'PP', 'address': ' '.join(
                                     x for x in (holder.get('address'), holder.get('postal_code'), holder.get('city')) if x),
                                 'birth_date': holder.get('birth_date') or '', 'gross': 0.0, 'pfnl': 0.0, 'ps': 0.0})
        b['gross'] += amount
        if b['kind'] != 'PM':
            if k not in dispensed:
                b['pfnl'] += amount * PFNL_RATE / 100
            b['ps'] += amount * social_levies_rate(on) / 100

    unallocated = 0.0
    for l in lines:
        on = date.fromisoformat(str(l['entry_date'])[:10])
        amount = float(l['debit'] or 0)
        h = by_key.get(_key(l['auxiliary_name'])) if l['auxiliary_name'] else None
        if h:
            add(h, amount, on)
        elif total_pct > 0:
            for hh in holders:
                if float(hh.get('pct') or 0):
                    add(hh, amount * float(hh['pct']) / total_pct, on)
        else:
            unallocated += amount
    rows = []
    for b in sorted(benef.values(), key=lambda x: -x['gross']):
        b = dict(b, gross=_r(b['gross']), pfnl=_r(b['pfnl']), ps=_r(b['ps']), dispensed=b['key'] in dispensed)
        if b['kind'] == 'PM':
            b.update({'box_2DC': 0.0, 'box_2CK': 0.0, 'box_2BH': 0.0})
        else:
            b.update({'box_2DC': b['gross'], 'box_2CK': b['pfnl'], 'box_2BH': b['gross']})
        rows.append(b)
    issues = []
    for b in rows:
        if not b['address']:
            issues.append(f"{b['name']} : adresse manquante (profil fiscal, composition du capital).")
        if b['kind'] != 'PM' and not b['birth_date']:
            issues.append(f"{b['name']} : date de naissance manquante.")
    if unallocated:
        amount_txt = f"{unallocated:,.2f}".replace(',', ' ').replace('.', ',')
        issues.append(f"{amount_txt} € de dividendes ne peuvent pas être attribués : renseignez la composition du "
                      "capital dans le profil fiscal ou le nom de l'associé sur l'écriture de paiement.")
    return {'year': year, 'rows': rows, 'total': _r(sum(b['gross'] for b in rows) + unallocated),
            'unallocated': _r(unallocated), 'issues': issues, 'deadline': ifu_deadline(year)}

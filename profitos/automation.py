"""Automatisations comptables et fiscales.

- Écritures récurrentes (loyers, abonnements, dotations mensuelles…) : modèles en partie
  double, générés automatiquement à chaque échéance par la tâche quotidienne
  « recurring_entries » (une seule écriture par modèle et par date, même si la tâche
  est relancée).
- Échéancier fiscal : TVA, acomptes et solde d'IS, liasse, CVAE, avec l'état de dépôt de
  chaque déclaration ; sert aux rappels automatiques (tâche « fiscal_deadlines »).
- Contrôles avant liasse : anomalies propres à la production fiscale (IS non
  comptabilisé, TVA non liquidée, comptes de sens anormal, tiers de sens inverse,
  exercice précédent non clôturé…), en complément des diagnostics de révision.
"""
import json
from datetime import date, timedelta

FREQUENCIES = {'monthly': ('Mensuelle', 1), 'quarterly': ('Trimestrielle', 3), 'yearly': ('Annuelle', 12)}


class AutomationError(ValueError):
    pass


def _d(value):
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _add_months(d, months, dom):
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    last = (date(y + (m // 12), m % 12 + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(dom, last))


# --------------------------------------------------------------- écritures récurrentes
def validate_lines(conn, lines):
    """Lignes {'account_code','label','debit','credit'} : au moins deux, comptes existants,
    montants positifs, total débit = total crédit."""
    clean = []
    for ln in lines:
        code = (ln.get('account_code') or '').strip()
        debit = round(float(ln.get('debit') or 0), 2)
        credit = round(float(ln.get('credit') or 0), 2)
        if not code and not debit and not credit:
            continue
        if not code:
            raise AutomationError('Chaque ligne doit indiquer un compte.')
        if debit < 0 or credit < 0 or (debit and credit) or not (debit or credit):
            raise AutomationError(f'Ligne {code} : indiquez soit un débit, soit un crédit positif.')
        if not conn.execute('SELECT 1 FROM accounting_chart_of_accounts WHERE code=?', (code,)).fetchone():
            raise AutomationError(f'Compte inconnu : {code}.')
        clean.append({'account_code': code, 'label': (ln.get('label') or '').strip()[:120], 'debit': debit, 'credit': credit})
    if len(clean) < 2:
        raise AutomationError('Une écriture comporte au moins deux lignes.')
    if abs(sum(x['debit'] for x in clean) - sum(x['credit'] for x in clean)) >= 0.005:
        raise AutomationError("L'écriture n'est pas équilibrée : total des débits différent du total des crédits.")
    return clean


def create_recurring_entry(conn, entity_id, label, journal_code, lines, frequency, start_date, end_date=None,
                           created_by=None, now_iso=None):
    if frequency not in FREQUENCIES:
        raise AutomationError('Fréquence invalide.')
    label = (label or '').strip()
    if not label:
        raise AutomationError('Le libellé est obligatoire.')
    if not conn.execute('SELECT 1 FROM accounting_journals WHERE code=?', (journal_code,)).fetchone():
        raise AutomationError('Journal inconnu.')
    start = _d(start_date)
    end = _d(end_date) if end_date else None
    if end and end < start:
        raise AutomationError('La date de fin précède la date de début.')
    clean = validate_lines(conn, lines)
    conn.execute(
        """INSERT INTO recurring_entries(entity_id,label,journal_code,lines_json,frequency,day_of_month,start_date,end_date,
               next_date,active,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,1,?,?)""",
        (entity_id, label[:120], journal_code, json.dumps(clean, ensure_ascii=False), frequency, start.day,
         start.isoformat(), end.isoformat() if end else None, start.isoformat(), created_by, now_iso))
    conn.commit()
    return conn.execute('SELECT last_insert_rowid()').fetchone()[0]


def _ef(entity_id, alias=''):
    col = f'{alias}entity_id' if alias else 'entity_id'
    return (f'{col}=?', (entity_id,)) if entity_id else (f'{col} IS NULL', ())


def list_recurring_entries(conn, entity_id):
    ef, ep = _ef(entity_id)
    rows = conn.execute(f'SELECT * FROM recurring_entries WHERE {ef} ORDER BY active DESC, next_date', ep).fetchall()
    out = []
    for r in rows:
        x = dict(r)
        x['lines'] = json.loads(r['lines_json'] or '[]')
        x['amount'] = round(sum(l['debit'] for l in x['lines']), 2)
        x['frequency_label'] = FREQUENCIES.get(r['frequency'], (r['frequency'],))[0]
        out.append(x)
    return out


def generate_due_entries(conn, entity_id, today, created_by='ProfitOS (automatique)'):
    """Passe toutes les écritures récurrentes échues jusqu'à today (rattrapage inclus).
    Une période close (date de verrouillage) arrête le modèle concerné avec une erreur
    explicite, sans bloquer les autres."""
    from profitos.accounting import create_entry, _entry_already_exists, AccountingError
    today = _d(today)
    ef, ep = _ef(entity_id)
    created, errors = [], []
    rows = conn.execute(f'SELECT * FROM recurring_entries WHERE {ef} AND active=1 AND next_date<=?',
                        (*ep, today.isoformat())).fetchall()
    for r in rows:
        step = FREQUENCIES[r['frequency']][1]
        due = _d(r['next_date'])
        end = _d(r['end_date']) if r['end_date'] else None
        lines = json.loads(r['lines_json'] or '[]')
        guard = 0
        while due <= today and (end is None or due <= end) and guard < 120:
            guard += 1
            source_type = f"recurring_entry:{r['id']}"
            source_id = int(due.strftime('%Y%m%d'))
            if not _entry_already_exists(conn, source_type, source_id, entity_id):
                try:
                    entry_id = create_entry(conn, r['journal_code'], due, r['label'],
                                            [dict(l, label=l.get('label') or r['label']) for l in lines],
                                            source_type=source_type, source_id=source_id, created_by=created_by,
                                            entity_id=entity_id)
                    created.append({'template_id': r['id'], 'entry_id': entry_id, 'date': due.isoformat(), 'label': r['label']})
                except AccountingError as exc:
                    errors.append(f"{r['label']} ({due.strftime('%d/%m/%Y')}) : {exc}")
                    break
            due = _add_months(due, step, r['day_of_month'] or due.day)
        active = 0 if (end and due > end) else 1
        conn.execute('UPDATE recurring_entries SET next_date=?,active=?,last_generated=? WHERE id=?',
                     (due.isoformat(), active, today.isoformat(), r['id']))
        conn.commit()
    return created, errors


def deactivate_recurring_entry(conn, entity_id, template_id):
    ef, ep = _ef(entity_id)
    cur = conn.execute(f'UPDATE recurring_entries SET active=0 WHERE id=? AND {ef}', (template_id, *ep))
    conn.commit()
    return cur.rowcount


# ----------------------------------------------------------------- échéancier fiscal
def upcoming_deadlines(conn, entity_id, today, horizon_days=45):
    """Échéances fiscales des prochains jours, avec l'état de dépôt de la déclaration
    correspondante dans ProfitOS."""
    from profitos import ledger as L
    from profitos import corporate_tax as CT
    from profitos.liasse_normal import liasse_normale
    from profitos.vat_return import ca3_form
    from profitos.teledeclaration import active_declaration
    today = _d(today)
    limit = today + timedelta(days=horizon_days)
    items = []
    # TVA mensuelle : déclaration du mois précédent, à déposer à partir du 15 du mois.
    for back in (1, 0):
        first = (today.replace(day=1) - timedelta(days=1)).replace(day=1) if back else today.replace(day=1)
        last = (_add_months(first, 1, 1) - timedelta(days=1))
        due = _add_months(first, 1, 15)
        if today <= due <= limit or (back and due >= today - timedelta(days=10)):
            form = ca3_form(conn, first.isoformat(), last.isoformat(), entity_id)
            dep = active_declaration(conn, entity_id, 'TVA_CA3', first, last)
            items.append({'date': due, 'label': f"Déclaration de TVA de {first.strftime('%m/%Y')} (à partir du 15, selon votre SIREN)",
                          'form': '3310-CA3', 'type': 'TVA_CA3', 'start': first, 'end': last,
                          'amount': form['l32'] or None, 'deposited': dep})
    current = L.fiscal_year_for(conn, today, entity_id)
    closed = L.fiscal_year_for(conn, current['start'] - timedelta(days=1), entity_id)
    settings = CT.get_fiscal_settings(conn, entity_id, closed['end'].isoformat())
    l = liasse_normale(conn, closed['start'], closed['end'], entity_id, settings=settings)
    eligible = CT.pme_eligible(l['chiffre_affaires'], closed['start'], closed['end'],
                               settings.get('capital_libere'), settings.get('detention_pp'))
    acomptes = settings.get('acomptes_verses')
    if acomptes in (None, ''):
        acomptes = CT.acomptes_from_ledger(conn, closed['start'], closed['end'], entity_id)
    is_res = CT.compute_is(l['fiscal']['XN'], closed['start'], closed['end'], l['chiffre_affaires'], eligible,
                           credits=settings.get('credits_impot') or 0, acomptes=acomptes)
    cvae = CT.compute_cvae(l['va']['OX'], l['va']['OG'], closed['start'], closed['end'])
    type_of = {'2065': 'LIASSE', '2572': 'IS_2572', '2571': 'IS_2571', '1330-CVAE / 1329-DEF': 'CVAE_1330'}
    for e in CT.fiscal_calendar(closed['start'], closed['end'], is_res, cvae):
        if not (today <= e['date'] <= limit):
            continue
        t = type_of.get(e['form'])
        dep = active_declaration(conn, entity_id, t, closed['start'], closed['end']) if t and t != 'IS_2571' else None
        items.append(dict(e, type=t, start=closed['start'], end=closed['end'], deposited=dep))
    return sorted(items, key=lambda x: x['date'])


# ----------------------------------------------------------------- contrôles avant liasse
def precontrols(conn, fy_start, fy_end, entity_id=None, today=None):
    from profitos.liasse_normal import _balances
    from profitos.accounting import last_opening_date
    fy_start, fy_end = _d(fy_start), _d(fy_end)
    from profitos.accounting import paris_today
    today = _d(today) if today else paris_today()
    bal = _balances(conn, fy_start, fy_end, entity_id)
    out = []

    def add(level, label, detail=''):
        out.append({'level': level, 'label': label, 'detail': detail})

    closed = fy_end < today
    if closed:
        tax = sum(b['closing'] for c, b in bal.items() if c.startswith(('695', '696')))
        results = sum(-b['closing'] for c, b in bal.items() if c[0] in '67')
        if abs(tax) < 0.005 and results > 0:
            add('warning', "Aucune charge d'impôt sur les sociétés comptabilisée pour un exercice bénéficiaire",
                "Comptabilisez l'IS depuis l'écran « Impôt sur les sociétés et CVAE ».")
        next_day = (fy_end + timedelta(days=1)).isoformat()
        if last_opening_date(conn, next_day, entity_id) != next_day:
            add('info', "L'exercice n'est pas encore clôturé dans ProfitOS (à-nouveaux non générés).")
    vat_left = sum(b['closing'] for c, b in bal.items() if c.startswith(('44566', '44562', '4457')))
    if abs(vat_left) >= 1:
        add('warning', 'Comptes de TVA déductible / collectée non soldés à la clôture',
            f"Solde de {abs(vat_left):,.0f} € : passez les écritures de liquidation des dernières périodes.".replace(',', ' '))
    wrong_sign = [c for c, b in bal.items() if (c[0] == '6' and b['closing'] < -0.5 and not c.startswith(('603', '609', '6097')))
                  or (c[0] == '7' and b['closing'] > 0.5 and not c.startswith(('713', '709')))]
    if wrong_sign:
        add('warning', 'Comptes de charges créditeurs ou de produits débiteurs', ', '.join(sorted(wrong_sign)[:10]))
    clients_cred = sum(-b['closing'] for c, b in bal.items() if c.startswith('411') and b['closing'] < -0.005)
    fourn_deb = sum(b['closing'] for c, b in bal.items() if c.startswith('401') and b['closing'] > 0.005)
    if clients_cred >= 1:
        add('info', 'Clients créditeurs reclassés en autres dettes', f"{clients_cred:,.0f} €".replace(',', ' '))
    if fourn_deb >= 1:
        add('info', 'Fournisseurs débiteurs reclassés en autres créances', f"{fourn_deb:,.0f} €".replace(',', ' '))
    bank_cred = sum(-b['closing'] for c, b in bal.items() if c.startswith('512') and b['closing'] < -0.005)
    if bank_cred >= 1:
        add('info', 'Banque créditrice présentée en concours bancaires (2051, DU)', f"{bank_cred:,.0f} €".replace(',', ' '))
    capital = sum(-b['closing'] for c, b in bal.items() if c.startswith(('101', '108')))
    if capital <= 0:
        add('warning', 'Capital social non comptabilisé (compte 101)', "Reprenez le capital dans les à-nouveaux ou par une écriture d'ouverture.")
    suspense = sum(abs(b['closing']) for c, b in bal.items() if c.startswith(('471', '472', '473', '474', '475')))
    if suspense >= 1:
        add('warning', "Comptes d'attente non soldés (471 à 475)", f"{suspense:,.0f} €".replace(',', ' '))
    return out

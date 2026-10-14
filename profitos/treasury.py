"""Sources de flux de la prévision de trésorerie, au-delà des factures.

Chaque source renvoie des flux datés et expliqués, sans jamais rien inventer :
  - échéances d'emprunts et de crédit-bail non réglées (tables loans / finance_leases) ;
  - factures récurrentes à venir (modèles d'abonnement clients actifs) ;
  - TVA à décaisser estimée d'après la comptabilité (régime réel, déclaration mensuelle) ;
  - flux prévus saisis par l'utilisateur (salaires, loyer, subvention, apport…) ;
  - habitudes de paiement de chaque client (retard moyen constaté sur ses factures réglées) ;
  - charges et recettes récurrentes repérées sur les relevés bancaires (suggestions à valider).

Un flux : {'date': 'AAAA-MM-JJ', 'amount': montant positif, 'direction': 'in'|'out',
           'category': clé de CATEGORIES, 'label': libellé, 'source': origine}.
"""
import calendar
import json
import re
import statistics
import unicodedata
from datetime import date, datetime, timedelta

HORIZON_DAYS = 90

CATEGORIES = {
    # encaissements
    'clients': ('in', 'Factures clients à encaisser'),
    'recurring_sales': ('in', 'Factures récurrentes à venir'),
    'planned_in': ('in', 'Autres encaissements prévus'),
    # décaissements
    'suppliers': ('out', 'Factures fournisseurs à payer'),
    'expenses': ('out', 'Dépenses futures enregistrées'),
    'payroll': ('out', 'Salaires et charges sociales'),
    'loans': ('out', 'Emprunts'),
    'leases': ('out', 'Crédit-bail'),
    'vat': ('out', 'TVA à décaisser (estimation)'),
    'planned_out': ('out', 'Autres décaissements prévus'),
    'burn': ('out', 'Dépense courante observée'),
}
CATEGORY_ORDER = ['clients', 'recurring_sales', 'planned_in', 'suppliers', 'expenses', 'payroll',
                  'loans', 'leases', 'vat', 'planned_out', 'burn']

# Catégories proposées à la saisie d'un flux prévu (clé → (sens, libellé, catégorie de la prévision)).
PLANNED_CATEGORIES = {
    'salaires': ('out', 'Salaires et charges sociales', 'payroll'),
    'loyer': ('out', 'Loyer et charges locatives', 'planned_out'),
    'impots': ('out', 'Impôts et taxes (IS, CFE…)', 'planned_out'),
    'abonnements': ('out', 'Abonnements et services', 'planned_out'),
    'financement_out': ('out', 'Remboursement de financement', 'planned_out'),
    'autre_out': ('out', 'Autre décaissement', 'planned_out'),
    'vente': ('in', 'Vente ou contrat signé', 'planned_in'),
    'subvention': ('in', 'Subvention, aide publique', 'planned_in'),
    'apport': ('in', 'Apport, levée de fonds, prêt reçu', 'planned_in'),
    'autre_in': ('in', 'Autre encaissement', 'planned_in'),
}
FREQUENCIES = {'once': 'Une fois', 'weekly': 'Chaque semaine', 'monthly': 'Chaque mois',
               'quarterly': 'Chaque trimestre', 'yearly': 'Chaque année'}

# Jour retenu pour le paiement de la TVA mensuelle : la date limite DGFiP tombe entre le 15
# et le 24 selon l'entreprise ; le 15 est retenu par prudence (décaissement le plus tôt).
VAT_PAYMENT_DAY = 15


def _d(value):
    try:
        return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
    except Exception:
        return None


def add_months(d, months):
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def occurrences(start, frequency, end, today, horizon_end, anchor_day=None):
    """Dates d'un flux répété entre aujourd'hui (inclus) et la fin d'horizon (incluse)."""
    start = _d(start)
    if not start:
        return []
    end = _d(end) if end else None
    out = []
    if frequency == 'once':
        if today <= start <= horizon_end and (not end or start <= end):
            out.append(start)
        return out
    step_days = 7 if frequency == 'weekly' else None
    months = {'monthly': 1, 'quarterly': 3, 'yearly': 12}.get(frequency, 1)
    current, i = start, 0
    while current <= horizon_end and i < 600:
        if end and current > end:
            break
        if current >= today:
            out.append(current)
        i += 1
        current = start + timedelta(days=step_days * i) if step_days else add_months(start, months * i)
    return out


def label_key(label):
    """Libellé normalisé pour reconnaître une même opération d'un mois à l'autre :
    majuscules sans accents, sans chiffres ni références, trois premiers mots utiles."""
    t = unicodedata.normalize('NFKD', str(label or '')).encode('ascii', 'ignore').decode().upper()
    t = re.sub(r'[^A-Z ]+', ' ', t)
    stop = {'PRLV', 'SEPA', 'VIR', 'VIREMENT', 'CB', 'CARTE', 'PRELEVEMENT', 'DE', 'DU', 'LA', 'LE', 'LES',
            'FACTURE', 'FACT', 'ECH', 'ECHEANCE', 'REF', 'INST', 'RECU', 'EMIS', 'EUR', 'SA', 'SAS', 'SARL'}
    words = [w for w in t.split() if len(w) > 1 and w not in stop]
    return ' '.join(words[:3])


# ---------------------------------------------------------------------- réglages
def _ekey(entity_id):
    return int(entity_id) if entity_id else 0


def get_settings(conn, entity_id):
    row = conn.execute('SELECT * FROM treasury_settings WHERE entity_key=?', (_ekey(entity_id),)).fetchone()
    dismissed = []
    if row and row['dismissed_suggestions']:
        try:
            dismissed = json.loads(row['dismissed_suggestions'])
        except ValueError:
            dismissed = []
    return {'safety_threshold': float(row['safety_threshold'] or 0) if row else 0.0,
            'include_observed_burn': bool(row['include_observed_burn']) if row and row['include_observed_burn'] is not None else True,
            'dismissed': dismissed}


def save_settings(conn, entity_id, now_iso, safety_threshold=None, include_observed_burn=None, dismissed=None):
    cur = get_settings(conn, entity_id)
    st = cur['safety_threshold'] if safety_threshold is None else max(0.0, float(safety_threshold))
    burn = cur['include_observed_burn'] if include_observed_burn is None else bool(include_observed_burn)
    dis = cur['dismissed'] if dismissed is None else dismissed
    conn.execute(
        """INSERT INTO treasury_settings(entity_key,safety_threshold,include_observed_burn,dismissed_suggestions,updated_at)
           VALUES(?,?,?,?,?) ON CONFLICT(entity_key) DO UPDATE SET safety_threshold=excluded.safety_threshold,
           include_observed_burn=excluded.include_observed_burn,dismissed_suggestions=excluded.dismissed_suggestions,
           updated_at=excluded.updated_at""",
        (_ekey(entity_id), st, 1 if burn else 0, json.dumps(dis[-200:]), now_iso))
    conn.commit()


# ---------------------------------------------------------------------- sources
def _flow(day, amount, direction, category, label, source, **extra):
    item = {'date': day.isoformat(), 'amount': round(float(amount), 2), 'direction': direction,
            'category': category, 'label': label, 'source': source}
    item.update(extra)
    return item


def loan_flows(conn, entity_id, today, horizon_end):
    rows = conn.execute(
        """SELECT i.due_date,i.capital_amount,i.interest_amount,i.installment_number,l.lender_name
             FROM loan_installments i JOIN loans l ON l.id=i.loan_id
            WHERE l.entity_id IS ? AND COALESCE(l.status,'active')='active' AND COALESCE(i.paid,0)=0
              AND i.due_date<=? ORDER BY i.due_date""", (entity_id, horizon_end.isoformat())).fetchall()
    out = []
    for r in rows:
        d = _d(r['due_date'])
        if not d:
            continue
        amount = float(r['capital_amount'] or 0) + float(r['interest_amount'] or 0)
        if amount > 0.005:
            out.append(_flow(max(d, today), amount, 'out', 'loans',
                             f"Échéance n° {r['installment_number']} — emprunt {r['lender_name']}", 'loan',
                             overdue=d < today))
    return out


def lease_flows(conn, entity_id, today, horizon_end):
    rows = conn.execute(
        """SELECT p.due_date,p.amount,p.payment_number,l.lessor_name,l.asset_description
             FROM finance_lease_payments p JOIN finance_leases l ON l.id=p.lease_id
            WHERE l.entity_id IS ? AND COALESCE(l.status,'active')='active' AND COALESCE(p.paid,0)=0
              AND p.due_date<=? ORDER BY p.due_date""", (entity_id, horizon_end.isoformat())).fetchall()
    out = []
    for r in rows:
        d = _d(r['due_date'])
        if d and float(r['amount'] or 0) > 0.005:
            out.append(_flow(max(d, today), r['amount'], 'out', 'leases',
                             f"Loyer n° {r['payment_number']} — {r['asset_description']} ({r['lessor_name']})", 'lease',
                             overdue=d < today))
    return out


def _template_total(line_items):
    """Total TTC d'un modèle, calculé comme la facture qui sera émise (invoice_amounts)."""
    from profitos import invoice_amounts
    try:
        items = json.loads(line_items or '[]')
        for it in items:
            if 'line_total' not in it:  # modèle ancien : total de ligne recalculé
                it['line_total'] = invoice_amounts.money(it.get('quantity', 1)) * invoice_amounts.money(it.get('unit_price', 0))
        return round(float(invoice_amounts.totals(items)[2]), 2)
    except Exception:
        return 0.0


def _next_period(d, frequency, interval_count):
    n = max(1, int(interval_count or 1))
    if frequency == 'weekly':
        return d + timedelta(weeks=n)
    return add_months(d, {'yearly': 12, 'quarterly': 3}.get(frequency, 1) * n)


def recurring_sales_flows(conn, entity_id, today, horizon_end):
    """Factures d'abonnement pas encore émises : chaque prochaine émission du modèle,
    encaissée à l'échéance prévue (date d'émission + délai de paiement du modèle).
    Les brouillons déjà générés sont dans les factures et ne sont pas recomptés."""
    rows = conn.execute(
        "SELECT * FROM recurring_invoice_templates WHERE entity_id IS ? AND status='active'", (entity_id,)).fetchall()
    out = []
    for t in rows:
        total = _template_total(t['line_items'])
        run = _d(t['next_run_date'])
        if total <= 0.005 or not run:
            continue
        due_days = max(0, int(t['due_days'] or 0))
        end = _d(t['end_date']) if t['end_date'] else None
        for _ in range(200):
            pay = run + timedelta(days=due_days)
            if pay > horizon_end or (end and run > end):
                break
            if pay >= today:
                out.append(_flow(pay, total, 'in', 'recurring_sales',
                                 f"Abonnement {t['client_name']} (émission le {run:%d/%m/%Y})", 'recurring_invoice',
                                 customer=t['client_name']))
            run = _next_period(run, t['frequency'], t['interval_count'])
    return out


def planned_flows(conn, entity_id, today, horizon_end):
    rows = conn.execute("SELECT * FROM treasury_planned_flows WHERE entity_id IS ? AND COALESCE(active,1)=1 ORDER BY start_date",
                        (entity_id,)).fetchall()
    out = []
    for r in rows:
        cat = PLANNED_CATEGORIES.get(r['category'])
        direction = r['direction'] if r['direction'] in ('in', 'out') else (cat[0] if cat else 'out')
        category = cat[2] if cat else ('planned_in' if direction == 'in' else 'planned_out')
        for d in occurrences(r['start_date'], r['frequency'], r['end_date'], today, horizon_end):
            out.append(_flow(d, r['amount'], direction, category, r['label'], 'planned', planned_id=r['id']))
    return out


def list_planned(conn, entity_id):
    return conn.execute("SELECT * FROM treasury_planned_flows WHERE entity_id IS ? AND COALESCE(active,1)=1 ORDER BY direction DESC,start_date,id",
                        (entity_id,)).fetchall()


def _vat_regime(conn, entity_id):
    try:
        if entity_id:
            row = conn.execute('SELECT vat_regime FROM entities WHERE id=?', (entity_id,)).fetchone()
        else:
            row = conn.execute('SELECT vat_regime FROM company WHERE id=1').fetchone()
        return (row['vat_regime'] if row else None) or 'normal'
    except Exception:
        return 'normal'


def _vat_payable_settled(conn, entity_id, today):
    """Vrai si le compte 445510 « TVA à décaisser » est soldé à ce jour."""
    row = conn.execute(
        """SELECT COALESCE(SUM(l.debit-l.credit),0) n FROM accounting_entry_lines l
             JOIN accounting_entries e ON e.id=l.entry_id
            WHERE l.account_code='445510' AND e.entry_date<=? AND e.entity_id IS ?""",
        (today.isoformat(), entity_id)).fetchone()
    return float(row['n'] or 0) >= -0.005


def vat_flows(conn, entity_id, today, horizon_end):
    """TVA nette (collectée − déductible) des mois écoulés non encore payés, d'après les
    écritures comptables. Mois précédent : payé le 15 du mois courant (ou demain si cette
    date est passée et la déclaration pas encore liquidée) ; mois en cours : le 15 du mois
    suivant, sur la base des écritures déjà passées. Rien en franchise en base."""
    if _vat_regime(conn, entity_id) == 'franchise_293B':
        return []
    from profitos import vat_return
    out = []
    first_this = today.replace(day=1)
    prev_end = first_this - timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    periods = [(prev_start, prev_end, add_months(prev_start, 1).replace(day=VAT_PAYMENT_DAY), False),
               (first_this, today, add_months(first_this, 1).replace(day=VAT_PAYMENT_DAY), True)]
    months_fr = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août', 'septembre',
                 'octobre', 'novembre', 'décembre']
    for start, end, pay_day, partial in periods:
        try:
            form = vat_return.ca3_form(conn, start.isoformat(), end.isoformat(), entity_id)
        except Exception:
            continue
        due = round(float(form.get('l28') or 0), 2)
        if form.get('liquidated') and _vat_payable_settled(conn, entity_id, today):
            continue  # liquidée puis réglée : plus rien à décaisser
        if due <= 0.005:
            continue
        if pay_day < today:
            if today.day > 24:  # date limite dépassée : considérée comme payée
                continue
            pay_day = today
        if pay_day > horizon_end:
            continue
        label = f"TVA de {months_fr[start.month - 1]} {start.year}" + (" (écritures à ce jour)" if partial else "")
        out.append(_flow(pay_day, due, 'out', 'vat', label, 'vat', partial=partial))
    return out


def client_payment_delays(conn, entity_id, max_invoices=12):
    """Retard moyen de paiement par client (jours après l'échéance), sur ses dernières
    factures entièrement réglées. Seulement à partir de 2 factures, borné à [-15, 60]."""
    from profitos.runtime import norm
    rows = conn.execute(
        """SELECT i.id,i.client_name,i.due_date,MAX(p.payment_date) paid_on
             FROM outgoing_invoices i JOIN outgoing_invoice_payments p ON p.invoice_id=i.id
            WHERE i.entity_id IS ? AND i.status='paid' AND i.due_date IS NOT NULL
            GROUP BY i.id,i.client_name,i.due_date ORDER BY MAX(p.payment_date) DESC""", (entity_id,)).fetchall()
    per = {}
    for r in rows:
        due, paid = _d(r['due_date']), _d(r['paid_on'])
        if not due or not paid:
            continue
        lst = per.setdefault(norm(r['client_name']), [])
        if len(lst) < max_invoices:
            lst.append((paid - due).days)
    out = {}
    for key, delays in per.items():
        if len(delays) >= 2:
            avg = statistics.mean(delays)
            out[key] = {'delay': int(round(max(-15, min(60, avg)))), 'count': len(delays)}
    return out


# ---------------------------------------------------------------------- banque
def _bank_tx_rows(conn, entity_id, since):
    return conn.execute(
        """SELECT t.id,t.transaction_date,t.label,t.amount,a.name account_name FROM bank_transactions t
             JOIN bank_accounts a ON a.provider=t.provider AND a.provider_account_id=t.provider_account_id
            WHERE a.entity_id IS ? AND t.transaction_date>=? AND COALESCE(t.raw_status,'')<>'deleted'
              AND (t.provider<>'swan' OR COALESCE(t.raw_status,'')='Booked')
            ORDER BY t.transaction_date""", (entity_id, since.isoformat())).fetchall()


def _guess_category(key, direction):
    k = key.upper()
    if direction == 'in':
        return 'subvention' if any(w in k for w in ('SUBVENTION', 'BPI', 'REGION', 'ASP')) else 'vente'
    if any(w in k for w in ('URSSAF', 'SALAIRE', 'PAIE', 'RETRAITE', 'PREVOYANCE', 'MUTUELLE', 'AGIRC', 'ARRCO', 'DSN')):
        return 'salaires'
    if any(w in k for w in ('LOYER', 'BAIL', 'SCI', 'FONCIA', 'NEXITY')):
        return 'loyer'
    if any(w in k for w in ('DGFIP', 'IMPOT', 'TRESOR PUBLIC', 'CFE', 'SIE')):
        return 'impots'
    if any(w in k for w in ('PRET', 'ECHEANCE PRET', 'CREDIT', 'LEASING', 'LOCATION')):
        return 'financement_out'
    return 'abonnements'


def detect_recurring(conn, entity_id, today, existing_keys=(), dismissed=()):
    """Opérations bancaires qui reviennent chaque mois (au moins 3 mois distincts sur les
    4 derniers, montant stable à ±15 %, écart de 25 à 35 jours) : proposées comme flux
    prévus, jamais ajoutées sans validation."""
    rows = _bank_tx_rows(conn, entity_id, today - timedelta(days=125))
    # Encaissements d'un client déjà facturé dans ProfitOS : ses factures sont comptées dans
    # la prévision, une recette récurrente en plus les compterait deux fois.
    from profitos.runtime import norm
    clients = set()
    for table, col in (('outgoing_invoices', 'client_name'), ('invoices', 'customer')):
        try:
            clients.update(norm(r['n']) for r in conn.execute(
                f"SELECT DISTINCT {col} n FROM {table} WHERE entity_id IS ? AND {col} IS NOT NULL", (entity_id,)).fetchall())
        except Exception:
            conn.rollback()
    clients = {c for c in clients if len(c) >= 3}
    groups = {}
    for r in rows:
        amount = float(r['amount'] or 0)
        if abs(amount) < 1:
            continue
        key = label_key(r['label'])
        if not key:
            continue
        groups.setdefault((key, 'in' if amount > 0 else 'out'), []).append((_d(r['transaction_date']), abs(amount), r['label']))
    out = []
    for (key, direction), items in groups.items():
        items = [i for i in items if i[0]]
        months = {(d.year, d.month) for d, _, _ in items}
        if len(months) < 3 or key in existing_keys or f"{direction}:{key}" in dismissed:
            continue
        if direction == 'in' and any(c in norm(items[-1][2]) for c in clients):
            continue
        amounts = [a for _, a, _ in items]
        med = statistics.median(amounts)
        if med <= 0 or any(abs(a - med) > 0.15 * med for a in amounts):
            continue
        dates = sorted(d for d, _, _ in items)
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        if not gaps or not all(25 <= g <= 35 for g in gaps):
            continue
        last = dates[-1]
        nxt = add_months(last, 1)
        while nxt < today:
            nxt = add_months(nxt, 1)
        out.append({'key': key, 'direction': direction, 'label': items[-1][2], 'amount': round(med, 2),
                    'occurrences': len(items), 'last_date': last.isoformat(), 'next_date': nxt.isoformat(),
                    'category': _guess_category(key, direction)})
    out.sort(key=lambda s: (s['direction'] == 'in', -s['amount']))
    return out[:12]


def realized_by_month(conn, entity_id, today, months=3):
    """Encaissements et décaissements réels des derniers mois, d'après les relevés."""
    start = add_months(today.replace(day=1), -months)
    rows = _bank_tx_rows(conn, entity_id, start)
    out = {}
    for r in rows:
        d = _d(r['transaction_date'])
        if not d:
            continue
        k = f"{d.year}-{d.month:02d}"
        m = out.setdefault(k, {'month': k, 'in': 0.0, 'out': 0.0, 'count': 0})
        a = float(r['amount'] or 0)
        if a >= 0:
            m['in'] += a
        else:
            m['out'] += -a
        m['count'] += 1
    for m in out.values():
        m['in'] = round(m['in'], 2); m['out'] = round(m['out'], 2); m['net'] = round(m['in'] - m['out'], 2)
        m['partial'] = m['month'] == f"{today.year}-{today.month:02d}"
    return [out[k] for k in sorted(out)]


def latest_bank_balance(conn, entity_id):
    """Somme des soldes bancaires connus de l'entité et date du plus ancien d'entre eux."""
    rows = conn.execute(
        """SELECT balance,statement_balance_date,last_synced_at FROM bank_accounts
            WHERE entity_id IS ? AND COALESCE(disabled,0)=0 AND balance IS NOT NULL
              AND provider IN ('powens','swan','import')""", (entity_id,)).fetchall()
    if not rows:
        return None
    dates = [str(r['statement_balance_date'] or r['last_synced_at'] or '')[:10] for r in rows]
    if not all(dates):
        return None
    return {'balance': round(sum(float(r['balance']) for r in rows), 2), 'as_of': min(dates), 'accounts': len(rows)}


def collect_flows(conn, entity_id, today):
    horizon_end = today + timedelta(days=HORIZON_DAYS)
    flows = []
    for fn in (loan_flows, lease_flows, recurring_sales_flows, planned_flows, vat_flows):
        try:
            flows.extend(fn(conn, entity_id, today, horizon_end))
        except Exception:
            # Une source indisponible (table absente sur une vieille base) ne bloque pas la prévision.
            try:
                conn.rollback()
            except Exception:
                pass
    return flows

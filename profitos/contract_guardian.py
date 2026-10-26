"""Contract Guardian (v481) : contrats fournisseurs et contrôle des factures reçues.

Registre des contrats fournisseurs (montant prévu par période, tolérance, indexation sur
un indice officiel de la veille, plafond de hausse, durée, tacite reconduction, préavis),
puis contrôles au format du moteur de vérification (profitos/verification.py) :

- écart de prix : facture HT au-delà du montant prévu, révisé par l'indice du contrat
  (valeurs officielles de la veille) et dans la tolérance convenue ;
- préavis : dernier jour pour résilier un contrat reconduit tacitement ;
- facture hors contrat : datée après la fin d'un contrat non reconduit ou résilié ;
- hausse sans contrat : fournisseur récurrent dont la dernière facture dépasse nettement
  l'habitude (aucun contrat enregistré pour trancher : « à vérifier ») ;
- doublon possible : même fournisseur, même montant, à quelques jours d'écart, ou même
  numéro de facture.
Aucune conclusion sans donnée : un indice indisponible ou non vérifié rend le contrôle
« à vérifier », jamais « conforme ».
"""
import re
import unicodedata
from datetime import date, timedelta

from profitos import verification as V

MODULE = 'contract_guardian'
PERIODICITIES = {'mensuel': ('Mensuel', 1), 'trimestriel': ('Trimestriel', 3), 'annuel': ('Annuel', 12),
                 'ponctuel': ('Ponctuel', None)}
NOTICE_WARNING_DAYS, NOTICE_URGENT_DAYS = 60, 15
DUPLICATE_WINDOW_DAYS = 10
LOOKBACK_DAYS = 365
_LEGAL = r'\b(sas|sasu|sarl|eurl|sa|sci|snc|sca|selarl|scop|ei|eirl|ste|societe|ets|etablissements)\b'


def _d(value):
    try:
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _fr(value):
    d = _d(value)
    return d.strftime('%d/%m/%Y') if d else '—'


def _money(x):
    from profitos.runtime import fr_number
    return f'{fr_number(x, 2)} €'


def _pct(x):
    return f"{float(x):.1f}".rstrip('0').rstrip('.').replace('.', ',') + ' %'


def supplier_key(name):
    """Nom de fournisseur normalisé (casse, accents, forme juridique, ponctuation)."""
    s = unicodedata.normalize('NFKD', str(name or '')).encode('ascii', 'ignore').decode().lower()
    s = re.sub(r'[^a-z0-9 ]+', ' ', s)
    s = re.sub(_LEGAL, ' ', s)
    return ' '.join(s.split())


def add_months(d, months):
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue


# ---------------------------------------------------------------------- stockage
def ensure_table(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS supplier_contracts(
        id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id INTEGER, supplier_id INTEGER, supplier_name TEXT NOT NULL,
        label TEXT NOT NULL, periodicity TEXT NOT NULL DEFAULT 'mensuel', amount_ht REAL, tolerance_pct REAL DEFAULT 2,
        index_code TEXT, index_base_period TEXT, cap_pct REAL, start_date TEXT, end_date TEXT,
        tacit_renewal INTEGER DEFAULT 0, renewal_months INTEGER DEFAULT 12, notice_days INTEGER DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'actif', terminated_on TEXT, notes TEXT, created_at TEXT, updated_at TEXT)''')
    conn.commit()


FIELDS = ('supplier_id', 'supplier_name', 'label', 'periodicity', 'amount_ht', 'tolerance_pct', 'index_code',
          'index_base_period', 'cap_pct', 'start_date', 'end_date', 'tacit_renewal', 'renewal_months', 'notice_days',
          'status', 'terminated_on', 'notes')


class ContractError(ValueError):
    pass


def clean(data):
    """Valide une saisie de contrat ; lève ContractError avec un message en français."""
    from profitos import market_watch as MW
    out = {k: data.get(k) for k in FIELDS}
    out['supplier_name'] = ' '.join(str(out['supplier_name'] or '').split())[:120]
    out['label'] = ' '.join(str(out['label'] or '').split())[:160]
    if not out['supplier_name'] or not out['label']:
        raise ContractError('Fournisseur et objet du contrat obligatoires.')
    if out['periodicity'] not in PERIODICITIES:
        raise ContractError('Périodicité inconnue.')
    for k in ('amount_ht', 'tolerance_pct', 'cap_pct'):
        v = out[k]
        out[k] = None if v in (None, '') else float(v)
        if out[k] is not None and out[k] < 0:
            raise ContractError('Les montants et pourcentages doivent être positifs.')
    if out['tolerance_pct'] is None:
        out['tolerance_pct'] = 2.0
    if out['tolerance_pct'] > 50 or (out['cap_pct'] or 0) > 100:
        raise ContractError('Tolérance ou plafond de hausse invraisemblable.')
    for k in ('start_date', 'end_date', 'terminated_on'):
        if out[k] in (None, ''):
            out[k] = None
        elif not _d(out[k]):
            raise ContractError('Date invalide.')
        else:
            out[k] = _d(out[k]).isoformat()
    if out['start_date'] and out['end_date'] and out['end_date'] < out['start_date']:
        raise ContractError('La fin du contrat précède son début.')
    out['tacit_renewal'] = 1 if out['tacit_renewal'] in (1, '1', True, 'on') else 0
    for k, default, hi in (('renewal_months', 12, 120), ('notice_days', 0, 730)):
        v = out[k]
        out[k] = default if v in (None, '') else int(float(v))
        if not 0 <= out[k] <= hi:
            raise ContractError('Durée de reconduction ou préavis invraisemblable.')
    if out['tacit_renewal'] and not out['end_date']:
        raise ContractError("Indiquez la date de fin de la période en cours pour suivre la reconduction.")
    out['index_code'] = (out['index_code'] or '').strip() or None
    if out['index_code']:
        defn = MW.definition(out['index_code'])
        if not MW.is_price_index(defn):
            raise ContractError("Indice d'indexation inconnu ou inadapté (un indice de prix est attendu).")
        if not re.fullmatch(r'\d{4}-(\d{2}|Q[1-4])', str(out['index_base_period'] or '')):
            raise ContractError("Indiquez la période de référence de l'indice (ex. 2026-01 ou 2026-Q1).")
    else:
        out['index_base_period'] = None
    out['status'] = out['status'] if out['status'] in ('actif', 'resilie') else 'actif'
    out['notes'] = (out['notes'] or '')[:1000] or None
    out['supplier_id'] = int(out['supplier_id']) if str(out['supplier_id'] or '').isdigit() else None
    return out


def save(conn, entity_id, data, now_iso, contract_id=None):
    ensure_table(conn)
    row = clean(data)
    if contract_id:
        sets = ','.join(f'{k}=?' for k in FIELDS)
        conn.execute(f'UPDATE supplier_contracts SET {sets}, updated_at=? WHERE id=? AND entity_id IS ?',
                     (*[row[k] for k in FIELDS], now_iso, contract_id, entity_id))
    else:
        cols = ','.join(FIELDS)
        conn.execute(f'INSERT INTO supplier_contracts(entity_id,{cols},created_at,updated_at) VALUES(?,{",".join("?" * len(FIELDS))},?,?)',
                     (entity_id, *[row[k] for k in FIELDS], now_iso, now_iso))
    conn.commit()
    return row


def contracts(conn, entity_id):
    ensure_table(conn)
    return [dict(r) for r in conn.execute('SELECT * FROM supplier_contracts WHERE entity_id IS ? ORDER BY supplier_name, id',
                                          (entity_id,)).fetchall()]


def get(conn, entity_id, contract_id):
    ensure_table(conn)
    r = conn.execute('SELECT * FROM supplier_contracts WHERE id=? AND entity_id IS ?', (contract_id, entity_id)).fetchone()
    return dict(r) if r else None


def delete(conn, entity_id, contract_id):
    ensure_table(conn)
    conn.execute('DELETE FROM supplier_contracts WHERE id=? AND entity_id IS ?', (contract_id, entity_id))
    conn.commit()


# ------------------------------------------------------------------- calendrier
def current_term(k, today):
    """(fin de la période en cours, date limite de résiliation) ou (None, None).
    Avec tacite reconduction, la fin avance par périodes de reconduction."""
    end = _d(k.get('end_date'))
    if not end:
        return None, None
    if k.get('tacit_renewal') and k.get('status') != 'resilie':
        months = int(k.get('renewal_months') or 12) or 12
        guard = 0
        while end < today and guard < 600:
            end = add_months(end, months)
            guard += 1
    deadline = end - timedelta(days=int(k.get('notice_days') or 0))
    return end, deadline


def covers(k, d, today):
    """Le contrat couvre-t-il une facture du jour d ?"""
    start = _d(k.get('start_date'))
    if start and d < start:
        return False
    if k.get('status') == 'resilie' and _d(k.get('terminated_on')) and d > _d(k['terminated_on']):
        return False
    end = _d(k.get('end_date'))
    if end and not k.get('tacit_renewal') and d > end:
        return False
    return True


# --------------------------------------------------------------------- montants
def expected_amount(k, invoice_date, ac):
    """Montant HT attendu pour une facture : (montant, contrôles, preuves). Indexé : montant ×
    indice du mois de facture / indice de référence (valeurs officielles de la veille)."""
    from profitos import market_watch as MW
    base = float(k['amount_ht'])
    checks, evidence = [], [V.evidence('Montant du contrat', _money(base) + ' HT', f"Contrat « {k['label']} »")]
    amount = base
    if k.get('index_code'):
        defn = MW.definition(k['index_code'])
        meta = MW.series_meta(ac, k['index_code']) or {}
        obs = MW.observations(ac, k['index_code'])
        values = dict(obs)
        ref = values.get(k['index_base_period'])
        cur = MW._value_at_or_before(obs, invoice_date)
        if meta.get('status') != 'ok' or ref is None or not cur:
            why = ('indice suspendu ou non mis à jour' if meta.get('status') != 'ok' else
                   f"valeur de référence {MW.period_label(k['index_base_period'])} absente" if ref is None else
                   'aucune valeur publiée à la date de la facture')
            checks.append(V.check("Indice du contrat vérifié", None, f"{defn['label']} : {why}.", 'Veille (INSEE)'))
            return None, checks, evidence
        amount = base * cur[1] / ref
        checks.append(V.check("Indice du contrat vérifié", True,
                              f"{defn['label']} : {MW.period_label(cur[0])} = {cur[1]:g} pour {MW.period_label(k['index_base_period'])} = {ref:g}.",
                              'Veille (INSEE)'))
        evidence.append(V.evidence('Montant révisé par l\'indice', _money(amount) + ' HT', defn['label'], cur[0]))
    if k.get('cap_pct') is not None and _d(k.get('start_date')):
        years = max(0, (invoice_date - _d(k['start_date'])).days // 365)
        ceiling = base * (1 + float(k['cap_pct']) / 100) ** years
        if amount > ceiling:
            amount = ceiling
            evidence.append(V.evidence('Plafond de hausse', f"{k['cap_pct']:g} % par an → {_money(ceiling)} HT", 'Contrat'))
    return round(amount, 2), checks, evidence


# --------------------------------------------------------------------- détection
def _purchases(conn, entity_id, today):
    since = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    return [dict(r) for r in conn.execute(
        """SELECT * FROM purchase_invoices WHERE entity_id IS ? AND issue_date IS NOT NULL AND issue_date>=?
           AND COALESCE(status,'unpaid')<>'cancelled' AND COALESCE(validation_status,'approved')<>'rejected'
           ORDER BY issue_date, id""", (entity_id, since)).fetchall()]


def _matches(k, p):
    if k.get('supplier_id') and p.get('supplier_id'):
        return int(k['supplier_id']) == int(p['supplier_id'])
    return supplier_key(k['supplier_name']) == supplier_key(p['supplier_name'])


def _supplier_email(conn, k):
    try:
        if k.get('supplier_id'):
            r = conn.execute('SELECT email FROM suppliers WHERE id=?', (k['supplier_id'],)).fetchone()
        else:
            r = conn.execute('SELECT email FROM suppliers WHERE LOWER(name)=LOWER(?)', (k['supplier_name'],)).fetchone()
        return (r['email'] if r else None) or None
    except Exception:
        return None


def _dispute_mailto(email, k, p, expected):
    from urllib.parse import quote
    subject = f"Facture {p['invoice_number']} — écart avec notre contrat « {k['label']} »"
    body = (f"Bonjour,\n\nVotre facture {p['invoice_number']} du {_fr(p['issue_date'])} s'élève à "
            f"{_money(p['subtotal'])} HT, alors que notre contrat « {k['label']} » prévoit {_money(expected)} HT "
            f"pour cette période. Pourriez-vous nous adresser un avoir ou une facture rectificative ?\n\nCordialement")
    return f"mailto:{email}?subject={quote(subject)}&body={quote(body)}"


def detect(conn, entity_id, today, ac=None):
    """Constats Contract Guardian. ac : connexion à la base partagée (indices de la veille)."""
    from profitos.runtime import auth_cx
    own_ac = ac is None
    ac = ac or auth_cx()
    try:
        return _detect(conn, entity_id, today, ac)
    finally:
        if own_ac:
            ac.close()


def _detect(conn, entity_id, today, ac):
    out = []
    ks = contracts(conn, entity_id)
    purchases = _purchases(conn, entity_id, today)
    covered_ids = set()
    for k in ks:
        # 1. Préavis.
        end, deadline = current_term(k, today)
        if k['status'] == 'actif' and k.get('tacit_renewal') and deadline:
            left = (deadline - today).days
            if 0 <= left <= NOTICE_WARNING_DAYS:
                out.append(V.finding(
                    f"contrat:preavis:{k['id']}:{end.isoformat()}", MODULE, 'contrat_preavis',
                    'critique' if left <= NOTICE_URGENT_DAYS else 'important',
                    f"Contrat « {k['label']} » ({k['supplier_name']}) : résiliation possible jusqu'au {_fr(deadline)}",
                    amount=k.get('amount_ht'),
                    checks=[V.check('Échéance et préavis renseignés', True,
                                    f"Période en cours jusqu'au {_fr(end)}, préavis de {k['notice_days']} jour(s), "
                                    f"reconduction tacite de {k['renewal_months']} mois.", 'Contrat')],
                    evidence=[V.evidence('Jours restants', str(left), 'Calcul ProfitOS', today.isoformat())],
                    detail="Sans résiliation avant cette date, le contrat est reconduit automatiquement. Vérifiez s'il sert "
                           "encore et si son prix est compétitif.",
                    action={'kind': 'link', 'label': 'Revoir le contrat', 'endpoint': 'contract_guardian_edit',
                            'args': {'contract_id': k['id']}},
                    follow_up_days=max(1, left), ref={'contract_id': k['id'], 'deadline': deadline.isoformat()}))
        # 2. Factures du fournisseur : écart de prix et factures hors contrat.
        for p in purchases:
            if not _matches(k, p):
                continue
            pd = _d(p['issue_date'])
            if not covers(k, pd, today):
                if k['status'] == 'resilie' or (_d(k.get('end_date')) and not k.get('tacit_renewal')):
                    stop = _d(k.get('terminated_on')) if k['status'] == 'resilie' and k.get('terminated_on') else _d(k.get('end_date'))
                    if stop and pd > stop:
                        out.append(V.finding(
                            f"contrat:hors:{p['id']}", MODULE, 'contrat_hors_periode', 'important',
                            f"Facture {p['invoice_number']} ({p['supplier_name']}) datée après la fin du contrat « {k['label']} »",
                            amount=p['subtotal'],
                            checks=[V.check('Fin du contrat enregistrée', True, f"Contrat terminé le {_fr(stop)}.", 'Contrat'),
                                    V.check('Facture postérieure', True, f"Facture du {_fr(pd)}.", 'Achats')],
                            action={'kind': 'link', 'label': 'Ouvrir la facture', 'endpoint': 'purchase_detail',
                                    'args': {'purchase_id': p['id']}},
                            follow_up_days=14, ref={'purchase_id': p['id'], 'contract_id': k['id']}))
                        covered_ids.add(p['id'])
                continue
            covered_ids.add(p['id'])
            if k.get('amount_ht') is None or k['periodicity'] == 'ponctuel' or p.get('subtotal') is None:
                continue
            expected, checks, evidence = expected_amount(k, pd, ac)
            invoiced = round(float(p['subtotal'] or 0), 2)
            checks = [V.check('Montant HT de la facture', True, f"{_money(invoiced)} HT.", f"Facture {p['invoice_number']}")] + checks
            if expected is None:
                out.append(V.finding(f"contrat:ecart:{p['id']}", MODULE, 'contrat_ecart', 'info',
                                     f"Facture {p['invoice_number']} ({p['supplier_name']}) : comparaison au contrat impossible",
                                     amount=invoiced, checks=checks, evidence=evidence,
                                     action={'kind': 'link', 'label': 'Ouvrir la facture', 'endpoint': 'purchase_detail',
                                             'args': {'purchase_id': p['id']}},
                                     follow_up_days=30, ref={'purchase_id': p['id'], 'contract_id': k['id']}))
                continue
            limit = round(expected * (1 + float(k['tolerance_pct'] or 0) / 100), 2)
            if invoiced <= limit + 0.005:
                continue
            gap = round(invoiced - expected, 2)
            checks.append(V.check('Écart au-delà de la tolérance du contrat', True,
                                  f"+{_money(gap)} HT soit +{_pct(gap / expected * 100)} (tolérance {_pct(k['tolerance_pct'])}).",
                                  'Calcul ProfitOS'))
            evidence.append(V.evidence('Montant attendu', _money(expected) + ' HT', 'Contrat'))
            email = _supplier_email(conn, k)
            alts = [{'label': 'Écrire au fournisseur', 'url': _dispute_mailto(email, k, p, expected)}] if email else []
            alts.append({'label': 'Voir le contrat', 'endpoint': 'contract_guardian_edit', 'args': {'contract_id': k['id']}})
            out.append(V.finding(f"contrat:ecart:{p['id']}", MODULE, 'contrat_ecart',
                                 'important' if gap >= 100 or gap / expected >= 0.05 else 'info',
                                 f"Facture {p['invoice_number']} ({p['supplier_name']}) : {_money(gap)} HT au-dessus du contrat",
                                 amount=gap, checks=checks, evidence=evidence,
                                 detail='Demandez un avoir ou une facture rectificative avant de payer.',
                                 action={'kind': 'link', 'label': 'Ouvrir la facture', 'endpoint': 'purchase_detail',
                                         'args': {'purchase_id': p['id']}},
                                 alternatives=alts, follow_up_days=21,
                                 ref={'purchase_id': p['id'], 'contract_id': k['id'], 'expected': expected}))
    out += _recurring_without_contract(conn, entity_id, today, ks)
    out += _duplicates(purchases)
    return out


def _recurring_without_contract(conn, entity_id, today, ks):
    from profitos.routes.invoicing import _detect_recurring_suppliers
    rows = conn.execute("SELECT * FROM purchase_invoices WHERE entity_id IS ? AND issue_date IS NOT NULL", (entity_id,)).fetchall()
    known = {supplier_key(k['supplier_name']) for k in ks}
    out = []
    for r in _detect_recurring_suppliers(rows):
        if not r.get('anomaly') or supplier_key(r['supplier']) in known:
            continue
        last = r.get('last_amount') or 0
        usual = r['usual_amount'] or 0
        if (today - _d(r['last_date'])).days > 120:
            continue
        out.append(V.finding(
            f"contrat:hausse:{supplier_key(r['supplier'])}:{r['last_date']}", MODULE, 'hausse_sans_contrat', 'info',
            f"{r['supplier']} : dernière facture {_money(last)} TTC pour {_money(usual)} habituellement",
            amount=round(last - usual, 2),
            checks=[V.check('Rythme de facturation régulier', True, f"{r['occurrences']} factures, {r['frequency'].lower()}.", 'Achats'),
                    V.check('Hausse prévue par un contrat', None, "Aucun contrat enregistré pour ce fournisseur : impossible de dire "
                            "si la hausse est prévue.", 'Contrats fournisseurs')],
            action={'kind': 'link', 'label': 'Enregistrer le contrat', 'endpoint': 'contract_guardian_new',
                    'args': {'fournisseur': r['supplier']}},
            alternatives=[{'label': 'Voir les dépenses récurrentes', 'endpoint': 'purchase_recurring'}],
            follow_up_days=30, ref={'supplier': r['supplier']}))
    return out


def _duplicates(purchases):
    out, seen = [], set()
    by_supplier = {}
    for p in purchases:
        by_supplier.setdefault(supplier_key(p['supplier_name']), []).append(p)
    for items in by_supplier.values():
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                same_number = (a['invoice_number'] or '').strip().lower() == (b['invoice_number'] or '').strip().lower() \
                    and (a['invoice_number'] or '').strip()
                close = abs(float(a['total'] or 0) - float(b['total'] or 0)) < 0.01 and float(a['total'] or 0) > 0 and \
                    abs((_d(b['issue_date']) - _d(a['issue_date'])).days) <= DUPLICATE_WINDOW_DAYS
                if not (same_number or close):
                    continue
                key = f"contrat:doublon:{a['id']}:{b['id']}"
                if key in seen:
                    continue
                seen.add(key)
                why = (f"même numéro {a['invoice_number']}" if same_number else
                       f"même montant {_money(a['total'])} TTC à {abs((_d(b['issue_date']) - _d(a['issue_date'])).days)} jour(s) d'écart")
                out.append(V.finding(
                    key, MODULE, 'doublon', 'important',
                    f"Doublon possible : {a['supplier_name']}, factures {a['invoice_number']} et {b['invoice_number']}",
                    amount=b['total'],
                    checks=[V.check('Deux factures distinctes enregistrées', True, why + '.', 'Achats'),
                            V.check('Prestation facturée deux fois', None,
                                    'À confirmer : deux livraisons réelles peuvent avoir le même montant.', 'Achats')],
                    action={'kind': 'link', 'label': f"Ouvrir {b['invoice_number']}", 'endpoint': 'purchase_detail',
                            'args': {'purchase_id': b['id']}},
                    alternatives=[{'label': f"Ouvrir {a['invoice_number']}", 'endpoint': 'purchase_detail',
                                   'args': {'purchase_id': a['id']}}],
                    follow_up_days=14, ref={'ids': [a['id'], b['id']]}))
    return out


KINDS = ('contrat_preavis', 'contrat_ecart', 'contrat_hors_periode', 'hausse_sans_contrat', 'doublon')
KIND_LABELS = {'contrat_preavis': 'Contrats fournisseurs', 'contrat_ecart': 'Contrats fournisseurs',
               'contrat_hors_periode': 'Contrats fournisseurs', 'hausse_sans_contrat': 'Hausses de prix',
               'doublon': 'Doublons possibles'}


def contract_view(k, today):
    end, deadline = current_term(k, today)
    return dict(k, current_end=end, notice_deadline=deadline,
                periodicity_label=PERIODICITIES.get(k['periodicity'], (k['periodicity'],))[0])

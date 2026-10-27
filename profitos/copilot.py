"""Copilote (v488) : analyse financière et fiscale fondée sur les données réelles.

Principe : tous les chiffres sont calculés en code, numérotés (F1, F2…) avec leur source et
leur calcul. Le modèle d'IA ne fait que rédiger à partir de ces faits ; sa réponse est
ensuite vérifiée :
- chaque chiffre cité doit figurer parmi les faits (à l'arrondi près) ;
- chaque référence [Fn] doit exister ;
sinon la réponse est écartée et le rapport calculé est affiché, avec la raison. Sans clé
d'API ou au-delà du quota, le copilote affiche directement le rapport calculé.
Les problèmes à résoudre et les actions viennent de Mission Control (constats vérifiés).
"""
import json
import re
from datetime import date, timedelta

MONTHS = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août', 'septembre', 'octobre', 'novembre',
          'décembre']
SYSTEM = ("Tu es le copilote financier de ProfitOS pour une PME française. Tu réponds en français, de façon brève et "
          "concrète, UNIQUEMENT à partir des faits numérotés fournis (F1, F2…) et de la liste des problèmes vérifiés. "
          "Règles absolues : n'invente aucun chiffre, ne fais aucun calcul nouveau (n'additionne, ne soustrais et ne "
          "compare pas de montants qui ne figurent pas tels quels dans les faits), cite le fait utilisé entre crochets "
          "après chaque chiffre, par exemple [F3]. Si la question ne peut pas être tranchée avec ces faits, dis-le et "
          "indique quelle donnée manque. Pas de conseil d'investissement ni de conseil juridique ; renvoie vers "
          "l'expert-comptable pour les cas particuliers. Termine par les deux ou trois actions prioritaires, prises "
          "dans la liste des problèmes.")


def _money(x):
    from profitos.runtime import fr_number
    return f'{fr_number(x, 2)} €'


def _fr(d):
    try:
        return (d if isinstance(d, date) else date.fromisoformat(str(d)[:10])).strftime('%d/%m/%Y')
    except (TypeError, ValueError):
        return '—'


def _month_bounds(d):
    start = d.replace(day=1)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return start, nxt - timedelta(days=1)


class Facts:
    def __init__(self):
        self.items, self.missing = [], []

    def add(self, topic, label, value, raw, source, calc):
        fid = f'F{len(self.items) + 1}'
        self.items.append({'id': fid, 'topic': topic, 'label': label, 'value': value, 'raw': raw, 'source': source,
                           'calc': calc})
        return fid

    def unavailable(self, topic, label, exc):
        self.missing.append({'topic': topic, 'label': label, 'why': type(exc).__name__})

    def get(self, fid):
        return next((f for f in self.items if f['id'] == fid), None)


def _sales(c, eid, start, end):
    r = c.execute("""SELECT COALESCE(SUM(COALESCE(subtotal,0)-COALESCE(deposit_applied_subtotal,0)),0) s, COUNT(*) n
                     FROM outgoing_invoices WHERE entity_id IS ? AND COALESCE(status,'draft') NOT IN ('draft','cancelled')
                     AND issue_date>=? AND issue_date<=?""", (eid, start.isoformat(), end.isoformat())).fetchone()
    try:
        cn = c.execute("""SELECT COALESCE(SUM(subtotal),0) s FROM outgoing_credit_notes WHERE entity_id IS ?
                          AND COALESCE(status,'issued')='issued' AND issue_date>=? AND issue_date<=?""",
                       (eid, start.isoformat(), end.isoformat())).fetchone()['s'] or 0
    except Exception:
        c.rollback()
        cn = 0
    return round(float(r['s'] or 0) - float(cn), 2), int(r['n'] or 0)


def build(c, eid, today, cash=None, accounting=True, findings=None):
    """Dossier de faits du mois en cours. findings : constats Mission Control (déjà calculés)."""
    F = Facts()
    m_start, m_end = _month_bounds(today)
    p_end = m_start - timedelta(days=1)
    p_start = p_end.replace(day=1)
    period = f"{MONTHS[today.month - 1]} {today.year} (du 1er au {_fr(today)})"
    prev = f"{MONTHS[p_start.month - 1]} {p_start.year}"

    try:
        cur, n = _sales(c, eid, m_start, today)
        F.add('Ventes', f"Chiffre d'affaires facturé HT — {period}", _money(cur), cur, 'Factures émises ProfitOS',
              f"Somme HT des {n} facture(s) émise(s) du mois, acomptes déjà facturés et avoirs déduits")
        last, n2 = _sales(c, eid, p_start, p_end)
        F.add('Ventes', f"Chiffre d'affaires facturé HT — {prev} (mois complet)", _money(last), last, 'Factures émises ProfitOS',
              f"Même calcul sur le mois précédent ({n2} facture(s))")
    except Exception as exc:
        c.rollback()
        F.unavailable('Ventes', "Chiffre d'affaires", exc)

    try:
        r = c.execute("""SELECT COALESCE(SUM(amount),0) s, COUNT(*) n FROM outgoing_invoice_payments WHERE entity_id IS ?
                         AND payment_date>=? AND payment_date<=?""", (eid, m_start.isoformat(), today.isoformat())).fetchone()
        F.add('Encaissements', f"Encaissements clients — {period}", _money(r['s'] or 0), round(float(r['s'] or 0), 2),
              'Règlements enregistrés', f"Somme des {int(r['n'] or 0)} règlement(s) client enregistré(s) depuis le 1er")
    except Exception as exc:
        c.rollback()
        F.unavailable('Encaissements', 'Encaissements', exc)

    try:
        from profitos.receivables import open_receivables
        rows = [x for x in open_receivables(c, eid, today) if x['overdue']]
        total = round(sum(x['outstanding'] for x in rows), 2)
        F.add('Impayés', 'Factures clients échues non réglées', _money(total), total, 'Créances clients',
              f"Restant dû sur {len(rows)} facture(s) échue(s) (factures émises et importées, règlements et avoirs déduits)")
        F.add('Impayés', 'Nombre de factures clients échues', str(len(rows)), len(rows), 'Créances clients', 'Comptage')
        if rows:
            old = max(rows, key=lambda x: x['days_overdue'])
            F.add('Impayés', 'Plus ancien impayé', f"{old['invoice_number'] or 'sans numéro'} — {old['customer']} : "
                  f"{_money(old['outstanding'])}, {old['days_overdue']} jours de retard", old['outstanding'],
                  'Créances clients', 'Facture échue la plus ancienne')
    except Exception as exc:
        c.rollback()
        F.unavailable('Impayés', 'Créances clients', exc)

    try:
        r = c.execute("""SELECT COALESCE(SUM(subtotal),0) s, COUNT(*) n FROM purchase_invoices WHERE entity_id IS ?
                         AND issue_date>=? AND issue_date<=? AND COALESCE(validation_status,'approved')<>'rejected'""",
                      (eid, m_start.isoformat(), today.isoformat())).fetchone()
        F.add('Achats', f"Achats fournisseurs HT — {period}", _money(r['s'] or 0), round(float(r['s'] or 0), 2),
              'Factures fournisseurs', f"Somme HT des {int(r['n'] or 0)} facture(s) fournisseur datée(s) du mois")
        r = c.execute("""SELECT COALESCE(SUM(p.total - COALESCE((SELECT SUM(x.amount) FROM purchase_invoice_payments x
                                                                 WHERE x.purchase_invoice_id=p.id),0)),0) s, COUNT(*) n
                         FROM purchase_invoices p WHERE p.entity_id IS ? AND COALESCE(p.status,'unpaid') NOT IN ('paid','cancelled')
                         AND COALESCE(p.validation_status,'approved')<>'rejected' AND p.due_date IS NOT NULL AND p.due_date<=?""",
                      (eid, (today + timedelta(days=30)).isoformat())).fetchone()
        F.add('Achats', 'Factures fournisseurs à payer sous 30 jours (échues comprises)', _money(r['s'] or 0),
              round(float(r['s'] or 0), 2), 'Factures fournisseurs', f"Restant à payer sur {int(r['n'] or 0)} facture(s)")
    except Exception as exc:
        c.rollback()
        F.unavailable('Achats', 'Achats', exc)

    try:
        from profitos import treasury as T
        bank = T.latest_bank_balance(c, eid)
        if bank:
            F.add('Trésorerie', 'Solde bancaire connu', f"{_money(bank['balance'])} au {_fr(bank['as_of'])}", bank['balance'],
                  'Banque', f"Somme des soldes de {bank['accounts']} compte(s)")
    except Exception as exc:
        c.rollback()
        F.unavailable('Trésorerie', 'Solde bancaire', exc)
    if cash:
        if cash.get('min_cash') is not None:
            when = _fr(today + timedelta(days=cash['min_day'])) if cash.get('min_day') is not None else '—'
            F.add('Trésorerie', 'Point bas prévu sur 90 jours', f"{_money(cash['min_cash'])} vers le {when}", cash['min_cash'],
                  'Trésorerie & scénarios', 'Prévision : solde de départ + encaissements attendus − décaissements prévus')
        F.add('Trésorerie', 'État de la prévision', {'ALERTE': 'Alerte', 'VIGILANCE': 'Vigilance', 'STABLE': 'Stable',
                                                     'INCOMPLET': 'Incomplète'}.get(cash.get('alert_level'), '—'),
              None, 'Trésorerie & scénarios', cash.get('alert') or '')

    if accounting:
        try:
            from profitos.vat_return import ca3_form
            form = ca3_form(c, p_start, p_end, eid)
            if form.get('l28'):
                F.add('TVA', f"TVA nette due — {prev} (estimation)", _money(form['l28']), form['l28'], 'Comptabilité (CA3)',
                      f"TVA collectée {_money(form['l16'])} − TVA déductible, d'après les écritures du mois")
            elif form.get('l25'):
                F.add('TVA', f"Crédit de TVA — {prev} (estimation)", _money(form['l25']), form['l25'], 'Comptabilité (CA3)',
                      'TVA déductible supérieure à la TVA collectée')
        except Exception as exc:
            c.rollback()
            F.unavailable('TVA', 'Estimation de TVA', exc)
        try:
            from profitos import automation as A
            soon = [i for i in A.upcoming_deadlines(c, eid, today, horizon_days=30) if not i.get('deposited') and i['date'] >= today]
            late = A.overdue_declarations(c, eid, today)
            F.add('Fiscalité', 'Échéances fiscales des 30 prochains jours', str(len(soon)), len(soon), 'Échéancier fiscal',
                  '; '.join(f"{i['label']} ({_fr(i['date'])})" for i in soon[:4]) or 'Aucune')
            F.add('Fiscalité', 'Déclarations en retard', str(len(late)), len(late), 'Échéancier fiscal',
                  '; '.join(f"{i['label']} (échéance {_fr(i['date'])})" for i in late[:4]) or 'Aucune')
        except Exception as exc:
            c.rollback()
            F.unavailable('Fiscalité', 'Échéancier fiscal', exc)

    problems = []
    for f in findings or []:
        if (f.get('track') or {}).get('status', 'ouvert') != 'ouvert':
            continue
        problems.append({'key': f['key'], 'title': f['title'], 'severity': f['severity'], 'level': f['level'],
                         'amount': f.get('amount'), 'amount_label': f.get('amount_label'),
                         'missing': [ch for ch in f['checks'] if ch['ok'] is not True],
                         'action': (f.get('action') or {}).get('label')})
    order = {'critique': 0, 'important': 1, 'info': 2}
    problems.sort(key=lambda p: (order.get(p['severity'], 3), -(p['amount'] or 0)))
    for p in problems:
        if p['amount'] is not None:
            p['fact'] = F.add('Problèmes', f"{p['title']} — {p['amount_label']}", _money(p['amount']), p['amount'],
                              'Mission Control', 'Constat vérifié')
    return {'facts': F.items, 'missing': F.missing, 'problems': problems, 'period': period, 'prev': prev}


# ------------------------------------------------------------------ rapport calculé
def report(dossier):
    """Synthèse rédigée sans IA, uniquement à partir des faits (toujours disponible)."""
    facts = {f['label']: f for f in dossier['facts']}
    lines = []

    sales = next((f for f in dossier['facts'] if f['topic'] == 'Ventes'), None)
    prev = next((f for f in dossier['facts'] if f['topic'] == 'Ventes' and 'mois complet' in f['label']), None)
    if sales:
        lines.append(f"Ventes : {sales['value']} facturés HT sur {dossier['period']} [{sales['id']}]" +
                     (f", pour {prev['value']} sur {dossier['prev']} complet [{prev['id']}]." if prev else '.'))
    enc = next((f for f in dossier['facts'] if f['topic'] == 'Encaissements'), None)
    unpaid = next((f for f in dossier['facts'] if f['label'] == 'Factures clients échues non réglées'), None)
    count = next((f for f in dossier['facts'] if f['label'] == 'Nombre de factures clients échues'), None)
    if enc or unpaid:
        txt = 'Encaissements : '
        if enc:
            txt += f"{enc['value']} reçus depuis le 1er [{enc['id']}]"
        if unpaid:
            txt += (' ; ' if enc else '') + f"{unpaid['value']} de factures échues non réglées" + \
                (f" ({count['value']} facture(s)) [{unpaid['id']}][{count['id']}]" if count else f" [{unpaid['id']}]")
        lines.append(txt + '.')
    bank = facts.get('Solde bancaire connu')
    low = facts.get('Point bas prévu sur 90 jours')
    state = facts.get('État de la prévision')
    if bank or low:
        txt = 'Trésorerie : '
        if bank:
            txt += f"solde de {bank['value']} [{bank['id']}]"
        if low:
            txt += (' ; ' if bank else '') + f"point bas prévu de {low['value']} [{low['id']}]"
        if state:
            txt += f" — prévision « {state['value']} » [{state['id']}]"
        lines.append(txt + '.')
    pay = facts.get('Factures fournisseurs à payer sous 30 jours (échues comprises)')
    if pay and pay['raw']:
        lines.append(f"Fournisseurs : {pay['value']} à payer sous 30 jours [{pay['id']}].")
    vat = next((f for f in dossier['facts'] if f['topic'] == 'TVA'), None)
    if vat:
        lines.append(f"TVA : {vat['label'].split(' — ')[0].lower()} de {vat['value']} pour {dossier['prev']} [{vat['id']}], "
                     "estimation d'après la comptabilité.")
    soon = facts.get('Échéances fiscales des 30 prochains jours')
    late = facts.get('Déclarations en retard')
    if soon or late:
        txt = 'Fiscalité : '
        if late and late['raw']:
            txt += f"{late['value']} déclaration(s) en retard [{late['id']}] ; "
        if soon:
            txt += f"{soon['value']} échéance(s) dans les 30 jours [{soon['id']}]"
        lines.append(txt.rstrip(' ;') + '.')
    if dossier['problems']:
        top = dossier['problems'][:3]
        lines.append('Priorités : ' + ' ; '.join(p['title'] + (f" [{p['fact']}]" if p.get('fact') else '') for p in top) + '.')
    else:
        lines.append("Mission Control ne signale aucun problème ouvert.")
    if dossier['missing']:
        lines.append('Non disponible : ' + ', '.join(m['label'] for m in dossier['missing']) + '.')
    return lines


# ---------------------------------------------------------------------- garde-fou
_DATE = re.compile(r'\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b')
_REF = re.compile(r'\[F(\d+)\]')
_NUM = re.compile(r'(?<![\w])\d{1,3}(?:[  \xa0]\d{3})+(?:,\d+)?|(?<![\w])\d+(?:[.,]\d+)?')


def _to_float(s):
    s = s.replace(' ', '').replace(' ', '').replace('\xa0', '')
    if ',' in s:
        s = s.replace('.', '').replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return None


def check_answer(text, dossier):
    """(ok, raisons) : chaque référence existe et chaque chiffre figure dans les faits."""
    reasons = []
    ids = {f['id'] for f in dossier['facts']}
    refs = set(f"F{n}" for n in _REF.findall(text))
    unknown = sorted(refs - ids)
    if unknown:
        reasons.append('référence(s) inexistante(s) : ' + ', '.join(unknown))
    if not refs and _NUM.search(_REF.sub('', _DATE.sub('', text))):
        reasons.append('chiffres sans référence aux faits')
    allowed = set()
    for f in dossier['facts']:
        for candidate in [f['raw']] + [_to_float(x) for x in _NUM.findall(f"{f['value']} {f['label']} {f['calc']}")]:
            if isinstance(candidate, (int, float)) and candidate is not None:
                allowed.add(round(abs(float(candidate)), 2))
    from profitos.accounting import paris_today
    y0 = paris_today().year
    years = {y0 - 1, y0, y0 + 1}
    stripped = _REF.sub('', _DATE.sub('', text))
    bad = []
    for raw in _NUM.findall(stripped):
        v = _to_float(raw)
        if v is None or v in years or (v.is_integer() and v < 10):
            continue                                   # années, petits nombres (« 3 actions »)
        if not any(abs(v - a) <= 0.011 for a in allowed):
            bad.append(raw.strip())
    if bad:
        reasons.append('chiffre(s) absent(s) des faits : ' + ', '.join(sorted(set(bad))[:5]))
    return not reasons, reasons


# ---------------------------------------------------------------------------- IA
def ask_ai(question, dossier, http=None):
    """Réponse rédigée par le modèle configuré (ANTHROPIC_API_KEY), ou ValueError."""
    from profitos.runtime import ANTHROPIC_API_KEY, ANTHROPIC_MODEL
    if not ANTHROPIC_API_KEY:
        raise ValueError("L'IA n'est pas configurée sur ce serveur.")
    facts = [{'id': f['id'], 'thème': f['topic'], 'libellé': f['label'], 'valeur': f['value'], 'calcul': f['calc'],
              'source': f['source']} for f in dossier['facts']]
    problems = [{'titre': p['title'], 'gravité': p['severity'], 'fait': p.get('fact'), 'action proposée': p['action'],
                 'ce qui manque': [m['label'] + (f" : {m['fix']}" if m.get('fix') else '') for m in p['missing']]}
                for p in dossier['problems'][:15]]
    content = (f"Période : {dossier['period']}.\nFaits :\n{json.dumps(facts, ensure_ascii=False, indent=1)}\n"
               f"Problèmes vérifiés (Mission Control) :\n{json.dumps(problems, ensure_ascii=False, indent=1)}\n\n"
               f"Question : {question}")
    payload = {'model': ANTHROPIC_MODEL, 'max_tokens': 900, 'system': SYSTEM,
               'messages': [{'role': 'user', 'content': content}]}
    headers = {'x-api-key': ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'}
    if http is None:
        import requests
        resp = requests.post('https://api.anthropic.com/v1/messages', json=payload, headers=headers, timeout=60)
        status, body = resp.status_code, resp.json() if resp.content else {}
    else:
        status, body = http(payload, headers)
    if status != 200:
        raise ValueError(f"service d'IA indisponible (HTTP {status})")
    text = ''.join(part.get('text', '') for part in body.get('content', []) if part.get('type') == 'text').strip()
    if not text:
        raise ValueError("réponse vide du service d'IA")
    return text

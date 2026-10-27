"""Chaîne de traitement des factures fournisseurs (v486) : moteur d'automatisation central.

Pour chaque facture fournisseur, ProfitOS enchaîne dix étapes et montre le résultat de
chacune (format du moteur de vérification, profitos/verification.py) :

 1. Réception         — origine (facture électronique, PDF, boîte mail, saisie) et justificatif
 2. Mentions          — numéro, date, fournisseur identifié, montants cohérents, taux de TVA
 3. Doublons          — même numéro chez le même fournisseur, ou même montant à quelques jours
 4. Contrat           — comparaison au contrat fournisseur (Contract Guardian), indice compris
 5. Écriture          — écriture d'achat préparée (ou déjà passée et contrôlée), période ouverte
 6. TVA               — traitement applicable : déduction, franchise, autoliquidation,
                        carburant, services (déduction au paiement)…
 7. Trésorerie        — échéance intégrée à la prévision, solde bancaire suffisant
 8. Anomalies         — synthèse des points « ? » et « ✗ »
 9. Actions           — actions proposées (valider et comptabiliser, contester, payer…)
10. Exécution         — après validation humaine : exécution des actions autorisées, puis
                        contrôle du résultat sur les données (écriture passée et équilibrée,
                        statut à jour), consigné dans un journal.

Rien n'est exécuté sans validation ; tout est recalculé au moment de l'exécution.
"""
import re
from datetime import date, timedelta

from profitos import verification as V

FR_RATES = (20.0, 10.0, 5.5, 2.1)
EU_PREFIXES = {'AT', 'BE', 'BG', 'CY', 'CZ', 'DE', 'DK', 'EE', 'EL', 'GR', 'ES', 'FI', 'HR', 'HU', 'IE', 'IT', 'LT', 'LU',
               'LV', 'MT', 'NL', 'PL', 'PT', 'RO', 'SE', 'SI', 'SK', 'XI'}
SERVICE_CATEGORIES = {'logiciel_saas', 'assurance', 'telephone', 'sous_traitance', 'loyer'}
DUPLICATE_DAYS = 10
STEP_NAMES = ['Réception', 'Mentions obligatoires', 'Doublons', 'Contrat fournisseur', 'Écriture comptable', 'TVA',
              'Trésorerie', 'Anomalies', 'Actions proposées', 'Exécution et contrôle']
VALIDATOR_ROLES = ('OWNER', 'ADMIN', 'COMPTABLE')


def _d(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _fr(value):
    d = _d(value)
    return d.strftime('%d/%m/%Y') if d else '—'


def _money(x):
    from profitos.runtime import fr_number
    return f'{fr_number(x, 2)} €'


def _pct(x):
    return f"{x:.2f}".rstrip('0').rstrip('.').replace('.', ',') + ' %'


def _step(n, checks, summary=''):
    return {'n': n, 'name': STEP_NAMES[n - 1], 'checks': checks, 'level': V.level(checks) if checks else V.READY,
            'summary': summary}


def ensure_log(c):
    c.execute('''CREATE TABLE IF NOT EXISTS purchase_pipeline_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id INTEGER, purchase_id INTEGER NOT NULL, at TEXT, actor TEXT,
        action TEXT, ok INTEGER, result TEXT)''')
    c.commit()


def log_entries(c, eid, purchase_id):
    ensure_log(c)
    return [dict(r) for r in c.execute('SELECT * FROM purchase_pipeline_log WHERE entity_id IS ? AND purchase_id=? ORDER BY id DESC',
                                       (eid, purchase_id)).fetchall()]


def _log(c, eid, purchase_id, actor, action, ok, result):
    from profitos.runtime import now
    ensure_log(c)
    c.execute('INSERT INTO purchase_pipeline_log(entity_id,purchase_id,at,actor,action,ok,result) VALUES(?,?,?,?,?,?,?)',
              (eid, purchase_id, now(), actor, action, 1 if ok else 0, (result or '')[:1000]))
    c.commit()


# ------------------------------------------------------------------ données utiles
def supplier_info(c, p):
    try:
        if p.get('supplier_id'):
            r = c.execute('SELECT * FROM suppliers WHERE id=?', (p['supplier_id'],)).fetchone()
        else:
            r = c.execute('SELECT * FROM suppliers WHERE LOWER(name)=LOWER(?)', (p['supplier_name'],)).fetchone()
        return dict(r) if r else None
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        return None


def vat_country(vat_number):
    v = re.sub(r'[^A-Z0-9]', '', str(vat_number or '').upper())
    return v[:2] if len(v) >= 4 and v[:2].isalpha() else None


def entry_for(c, eid, purchase_id):
    r = c.execute("SELECT * FROM accounting_entries WHERE source_type='purchase_invoice' AND source_id=? AND entity_id IS ? LIMIT 1",
                  (purchase_id, eid)).fetchone()
    if not r:
        return None, []
    lines = [dict(x) for x in c.execute('SELECT * FROM accounting_entry_lines WHERE entry_id=? ORDER BY line_order', (r['id'],)).fetchall()]
    return dict(r), lines


def closed_until(c, eid):
    r = c.execute('SELECT closed_until FROM accounting_entity_closure WHERE entity_key=?', (int(eid) if eid else 0,)).fetchone()
    return r['closed_until'] if r and r['closed_until'] else None


# ------------------------------------------------------------------------ analyse
def analyse(c, eid, purchase_id, today, ac=None):
    """Les dix étapes pour une facture fournisseur, ou None si elle n'existe pas."""
    row = c.execute('SELECT * FROM purchase_invoices WHERE id=? AND entity_id IS ?', (purchase_id, eid)).fetchone()
    if not row:
        return None
    p = dict(row)
    paid = float(c.execute('SELECT COALESCE(SUM(amount),0) s FROM purchase_invoice_payments WHERE purchase_invoice_id=? AND entity_id IS ?',
                           (purchase_id, eid)).fetchone()['s'] or 0)
    sup = supplier_info(c, p)
    subtotal, vat, total = float(p['subtotal'] or 0), float(p['vat_amount'] or 0), float(p['total'] or 0)
    steps = []

    # 1. Réception
    origin = ('Facture électronique (plateforme agréée)' if p.get('weinvoice_invoice_id') else
              'PDF importé ou reçu par la boîte mail' if p.get('document_path') else 'Saisie manuelle')
    checks = [V.check('Facture reçue', True, f"{origin}, enregistrée le {_fr(p.get('created_at'))}.", 'Achats')]
    has_proof = bool(p.get('document_path') or p.get('weinvoice_invoice_id'))
    checks.append(V.check('Justificatif disponible', True if has_proof else None,
                          'Original électronique ou PDF conservé.' if has_proof else 'Aucun PDF ni original électronique.',
                          'Achats', fix=None if has_proof else
                          "Joignez le PDF de la facture : sans justificatif, la TVA n'est pas déductible."))
    if p.get('weinvoice_status'):
        bad = str(p['weinvoice_status']).lower() in ('rejected', 'refused', 'rejetee', 'refusee')
        checks.append(V.check('Statut de la facture électronique', False if bad else True, str(p['weinvoice_status']),
                              'Plateforme agréée', fix='Régularisez avec le fournisseur avant de comptabiliser.' if bad else None))
    steps.append(_step(1, checks, origin))

    # 2. Mentions obligatoires
    checks = [V.check('Numéro et date de facture', bool(p['invoice_number'] and p['issue_date']),
                      f"N° {p['invoice_number'] or '—'} du {_fr(p['issue_date'])}.", 'Facture',
                      fix=None if p['invoice_number'] and p['issue_date'] else 'Complétez le numéro et la date.'),
              V.check('Montants cohérents (HT + TVA = TTC)', abs(subtotal + vat - total) <= 0.011,
                      f"{_money(subtotal)} HT + {_money(vat)} TVA = {_money(total)} TTC.", 'Facture',
                      fix=None if abs(subtotal + vat - total) <= 0.011 else 'Corrigez les montants de la facture.')]
    ident = sup and (sup.get('siret') or sup.get('vat_number'))
    checks.append(V.check('Fournisseur identifié (SIRET ou n° de TVA)', True if ident else None,
                          (f"SIRET {sup.get('siret')}" if sup and sup.get('siret') else '') +
                          (f" · TVA {sup.get('vat_number')}" if sup and sup.get('vat_number') else '') if ident else
                          'Fiche fournisseur sans SIRET ni n° de TVA.', 'Fiche fournisseur',
                          fix=None if ident else "Complétez la fiche du fournisseur (SIRET, n° de TVA) : nécessaire pour "
                                                 "contrôler la TVA et les mentions."))
    if p.get('due_date') and p.get('issue_date') and p['due_date'] < p['issue_date']:
        checks.append(V.check('Échéance postérieure à la facture', False, f"Échéance {_fr(p['due_date'])} avant le {_fr(p['issue_date'])}.",
                              'Facture', fix="Corrigez la date d'échéance."))
    rate = round(vat / subtotal * 100, 2) if subtotal > 0 else None
    if rate is not None and vat > 0:
        known = any(abs(rate - r) <= 0.05 for r in FR_RATES)
        checks.append(V.check('Taux de TVA plausible', True if known else None,
                              f"Taux apparent {_pct(rate)}." + ('' if known else ' Ce n\'est pas un taux français unique : '
                                                                'plusieurs taux sur la facture, ou erreur de saisie.'),
                              'Calcul ProfitOS', fix=None if known else 'Vérifiez la ventilation de la TVA sur la facture.'))
    steps.append(_step(2, checks))

    # 3. Doublons
    same_number = c.execute("""SELECT id, issue_date, total FROM purchase_invoices WHERE entity_id IS ? AND id<>? AND
                               LOWER(supplier_name)=LOWER(?) AND LOWER(invoice_number)=LOWER(?)
                               AND COALESCE(validation_status,'approved')<>'rejected' LIMIT 1""",
                            (eid, purchase_id, p['supplier_name'], p['invoice_number'] or '')).fetchone()
    checks = []
    if same_number:
        checks.append(V.check('Numéro unique chez ce fournisseur', False,
                              f"Déjà enregistrée (facture #{same_number['id']} du {_fr(same_number['issue_date'])}, "
                              f"{_money(same_number['total'])} TTC).", 'Achats',
                              fix='Rejetez le doublon (l\'écriture éventuelle sera extournée) ou corrigez le numéro.'))
    else:
        checks.append(V.check('Numéro unique chez ce fournisseur', True, 'Aucune autre facture avec ce numéro.', 'Achats'))
        d0 = _d(p['issue_date'])
        near = None
        if d0:
            near = c.execute("""SELECT id, invoice_number, issue_date FROM purchase_invoices WHERE entity_id IS ? AND id<>?
                                AND LOWER(supplier_name)=LOWER(?) AND ABS(total-?)<0.01 AND total>0
                                AND issue_date>=? AND issue_date<=? AND COALESCE(validation_status,'approved')<>'rejected' LIMIT 1""",
                             (eid, purchase_id, p['supplier_name'], total, (d0 - timedelta(days=DUPLICATE_DAYS)).isoformat(),
                              (d0 + timedelta(days=DUPLICATE_DAYS)).isoformat())).fetchone()
        checks.append(V.check('Pas de facture identique à quelques jours', None if near else True,
                              (f"Facture {near['invoice_number']} du {_fr(near['issue_date'])} : même fournisseur, même montant."
                               if near else 'Aucune facture du même montant à moins de 10 jours.'), 'Achats',
                              fix="Vérifiez qu'il s'agit bien de deux prestations distinctes." if near else None))
    steps.append(_step(3, checks))

    # 4. Contrat fournisseur
    from profitos import contract_guardian as CG
    checks, contract = [], None
    pd = _d(p['issue_date']) or today
    for k in CG.contracts(c, eid):
        if CG._matches(k, p):
            contract = k
            break
    if not contract:
        checks.append(V.check('Contrat fournisseur', True, 'Aucun contrat enregistré pour ce fournisseur : rien à comparer.',
                              'Contrats fournisseurs'))
    elif not CG.covers(contract, pd, today):
        checks.append(V.check('Facture couverte par le contrat', False, f"Contrat « {contract['label']} » terminé ou pas encore commencé "
                              f"à la date de la facture.", 'Contrats fournisseurs', fix="Contestez la facture ou mettez le contrat à jour."))
    elif contract.get('amount_ht') is None or contract['periodicity'] == 'ponctuel':
        checks.append(V.check('Contrat fournisseur', True, f"Contrat « {contract['label']} » sans montant périodique à comparer.",
                              'Contrats fournisseurs'))
    else:
        from profitos.runtime import auth_cx
        own = ac is None
        ac = ac or auth_cx()
        try:
            expected, idx_checks, _ev = CG.expected_amount(contract, pd, ac)
        finally:
            if own:
                ac.close()
        checks += idx_checks
        if expected is not None:
            limit = round(expected * (1 + float(contract['tolerance_pct'] or 0) / 100), 2)
            over = subtotal > limit + 0.005
            checks.append(V.check('Montant conforme au contrat', False if over else True,
                                  f"{_money(subtotal)} HT facturés pour {_money(expected)} HT prévus "
                                  f"(tolérance {_pct(contract['tolerance_pct'] or 0)})." +
                                  (f" Écart : {_money(subtotal - expected)} HT." if over else ''), 'Contrats fournisseurs',
                                  fix='Demandez un avoir ou une facture rectificative avant de payer.' if over else None))
    steps.append(_step(4, checks, contract['label'] if contract else ''))

    # 5. Écriture comptable
    from profitos.accounting import _category_account
    entry, lines = entry_for(c, eid, purchase_id)
    charge = _category_account(c, p.get('category') or 'autre')
    proposed = [{'account_code': charge, 'label': 'Charge', 'debit': subtotal, 'credit': 0.0}]
    if vat:
        proposed.append({'account_code': '445660', 'label': 'TVA déductible', 'debit': vat, 'credit': 0.0})
    proposed.append({'account_code': '401000', 'label': f"Fournisseur {p['supplier_name']}", 'debit': 0.0, 'credit': total})
    checks = []
    lock = closed_until(c, eid)
    if entry:
        deb = round(sum(float(l['debit'] or 0) for l in lines), 2)
        cred_401 = round(sum(float(l['credit'] or 0) for l in lines if str(l['account_code']).startswith('401')), 2)
        ok = abs(deb - total) <= 0.011 and abs(cred_401 - total) <= 0.011
        checks.append(V.check('Écriture passée et cohérente avec la facture', ok,
                              f"Écriture du {_fr(entry['entry_date'])} (journal {entry['journal_code']}) : {_money(deb)} au débit, "
                              f"{_money(cred_401)} au crédit fournisseur.", 'Comptabilité',
                              fix=None if ok else "Rejetez puis ressaisissez la facture : l'écriture sera extournée et refaite."))
    elif (p.get('validation_status') or 'approved') == 'pending':
        locked = bool(lock and (p['issue_date'] or today.isoformat()) <= lock)
        checks.append(V.check('Période comptable ouverte', False if locked else True,
                              f"Période clôturée jusqu'au {_fr(lock)}." if locked else
                              (f"Ouverte (clôture au {_fr(lock)})." if lock else 'Aucune clôture enregistrée.'), 'Comptabilité',
                              fix="Rouvrez la période ou datez l'écriture dans la période ouverte (expert-comptable)." if locked else None))
        checks.append(V.check('Écriture préparée', True, f"Charge {charge}, TVA 445660, fournisseur 401000 : en attente de validation.",
                              'Comptabilité'))
    elif (p.get('validation_status') or 'approved') == 'rejected':
        checks.append(V.check('Facture rejetée', True, 'Aucune écriture (ou écriture extournée).', 'Achats'))
    else:
        checks.append(V.check('Écriture passée', False, 'Facture validée mais aucune écriture comptable trouvée.', 'Comptabilité',
                              fix="Validez à nouveau la facture depuis la chaîne de traitement pour générer l'écriture."))
    steps.append(_step(5, checks))

    # 6. TVA
    checks, treatment = [], 'deduction'
    country = vat_country(sup.get('vat_number') if sup else None)
    cat = p.get('category') or 'autre'
    if country and country != 'FR' and vat <= 0.005:
        treatment = 'autoliquidation'
        where = "établi dans l'UE" if country in EU_PREFIXES else 'hors UE'
        checks.append(V.check('Autoliquidation', None,
                              f"Fournisseur {where} ({country}) sans TVA facturée : "
                              "la TVA est due par votre entreprise (autoliquidation) et déductible en même temps.",
                              'Fiche fournisseur et facture',
                              fix="Passez la TVA autoliquidée (TVA due 4452 / TVA déductible 445662) et déclarez-la sur la CA3 ; "
                                  "ProfitOS ne la passe pas automatiquement : à confirmer avec votre expert-comptable."))
    elif country and country != 'FR' and vat > 0.005:
        checks.append(V.check('TVA facturée par un fournisseur étranger', None,
                              f"Fournisseur {country} avec {_money(vat)} de TVA : en principe, pas de TVA française facturée "
                              "entre professionnels de pays différents.", 'Fiche fournisseur et facture',
                              fix="Vérifiez la facture : la TVA étrangère ne se déduit pas sur la CA3 (procédure de remboursement)."))
    elif vat <= 0.005 and cat == 'sous_traitance':
        treatment = 'autoliquidation'
        checks.append(V.check('Autoliquidation (sous-traitance du bâtiment)', None,
                              "Sous-traitance sans TVA : dans le bâtiment, le donneur d'ordre autoliquide la TVA "
                              "(art. 283, 2 nonies du CGI) si la facture porte la mention « autoliquidation ».", 'Facture',
                              fix="Vérifiez la mention sur la facture ; si elle y figure, passez la TVA autoliquidée et déclarez-la sur la CA3."))
    elif vat <= 0.005:
        treatment = 'sans_tva'
        checks.append(V.check('Pas de TVA facturée', True, 'Fournisseur en franchise de TVA ou opération exonérée : rien à déduire.',
                              'Facture'))
    else:
        if cat == 'carburant':
            treatment = 'carburant'
            checks.append(V.check('Déductibilité du carburant', None,
                                  f"{_money(vat)} de TVA comptés à 100 % : la TVA sur le carburant d'un véhicule de tourisme n'est "
                                  "déductible qu'à 80 % (100 % pour un utilitaire).", 'Code général des impôts, art. 298',
                                  fix="Confirmez le type de véhicule ; pour un véhicule de tourisme, 20 % de la TVA est une charge."))
        elif cat == 'assurance':
            checks.append(V.check('TVA sur une assurance', None, f"{_money(vat)} de TVA sur une prime d'assurance, en principe exonérée.",
                                  'Facture', fix="Vérifiez la facture : une taxe sur les assurances n'est pas de la TVA déductible."))
        if cat in SERVICE_CATEGORIES:
            treatment = 'services' if treatment == 'deduction' else treatment
            when = ('payée, donc déductible' if paid >= total - 0.005 else
                    'non encore payée : déductible à la date du paiement')
            checks.append(V.check('Moment de la déduction (services)', True,
                                  f"Prestation de services : la TVA est déductible au paiement, sauf si le fournisseur a opté pour "
                                  f"les débits (mention sur la facture). Facture {when}.", 'Code général des impôts, art. 271'))
        else:
            checks.append(V.check('TVA déductible', True, f"{_money(vat)} déductibles (achat de biens : à la date de la facture).",
                                  'Facture'))
    steps.append(_step(6, checks, {'deduction': 'TVA déductible', 'services': 'Déduction au paiement', 'sans_tva': 'Sans TVA',
                                   'autoliquidation': 'Autoliquidation', 'carburant': 'Carburant (80 % / 100 %)'}[treatment]))

    # 7. Trésorerie
    checks = []
    due_left = round(max(0.0, total - paid), 2)
    if due_left <= 0.005:
        checks.append(V.check('Facture réglée', True, f"{_money(paid)} payés.", 'Règlements'))
    elif not p.get('due_date'):
        checks.append(V.check('Échéance connue', None, 'Sans échéance, la facture ne peut pas être placée dans la prévision.', 'Facture',
                              fix="Renseignez la date d'échéance."))
    else:
        due = _d(p['due_date'])
        horizon = due and (due - today).days <= 90
        checks.append(V.check('Intégrée à la prévision de trésorerie', True,
                              f"{_money(due_left)} à payer le {_fr(due)}" + (' (dans les 90 jours de la prévision).' if horizon else
                                                                             ' (au-delà des 90 jours de la prévision).'),
                              'Trésorerie & scénarios'))
        if horizon:
            from profitos import treasury as T
            try:
                bank = T.latest_bank_balance(c, eid)
            except Exception:
                bank = None
            if bank is None:
                checks.append(V.check('Solde bancaire suffisant', None, 'Solde bancaire inconnu.', 'Banque',
                                      fix='Reliez la banque ou importez un relevé.'))
            else:
                ok = bank['balance'] >= due_left
                checks.append(V.check('Solde bancaire suffisant', ok, f"Solde de {_money(bank['balance'])} au {_fr(bank['as_of'])}.",
                                      'Banque', fix=None if ok else 'Planifiez le paiement ou négociez un délai.'))
    steps.append(_step(7, checks))

    # 8. Anomalies
    issues = [dict(ch, step=s['name']) for s in steps for ch in s['checks'] if ch['ok'] is not True]
    blocking = [i for i in issues if i['ok'] is False]
    steps.append(_step(8, [V.check('Anomalies relevées', (False if blocking else None) if issues else True,
                                   f"{len(blocking)} bloquante(s), {len(issues) - len(blocking)} à vérifier." if issues else
                                   'Aucune anomalie.', 'Synthèse')]))

    # 9. Actions proposées
    status = p.get('validation_status') or 'approved'
    actions = []
    if status == 'pending':
        actions.append({'op': 'approve', 'label': 'Valider et comptabiliser', 'kind': 'execute',
                        'allowed': not blocking and not [i for i in issues if i['step'] in ('Doublons', 'Mentions obligatoires')]})
    if entry is None and status == 'approved':
        actions.append({'op': 'post_entry', 'label': "Générer l'écriture manquante", 'kind': 'execute',
                        'allowed': not [b for b in blocking if b['label'] != 'Écriture passée']})
    if contract and any(ch['label'] == 'Montant conforme au contrat' and ch['ok'] is False for s in steps for ch in s['checks']):
        actions.append({'op': 'dispute', 'label': 'Contester auprès du fournisseur', 'kind': 'link', 'allowed': True,
                        'endpoint': 'contract_guardian'})
    if not has_proof:
        actions.append({'op': 'attach', 'label': 'Joindre le justificatif', 'kind': 'link', 'allowed': True,
                        'endpoint': 'purchase_detail', 'args': {'purchase_id': purchase_id}})
    if same_number:
        actions.append({'op': 'reject', 'label': 'Rejeter le doublon', 'kind': 'link', 'allowed': True,
                        'endpoint': 'purchase_detail', 'args': {'purchase_id': purchase_id}})
    if status == 'approved' and due_left > 0.005 and p.get('due_date') and not blocking:
        actions.append({'op': 'pay', 'label': 'Préparer le virement', 'kind': 'link', 'allowed': True, 'endpoint': 'purchase_sepa_batch'})
    steps.append(_step(9, [V.check('Actions proposées', True,
                                   ', '.join(a['label'] for a in actions) if actions else 'Aucune action nécessaire.', 'ProfitOS')]))

    # 10. Exécution et contrôle (journal)
    log = log_entries(c, eid, purchase_id)
    last = log[0] if log else None
    steps.append(_step(10, [V.check('Dernière exécution contrôlée', (bool(last['ok']) if last else True),
                                    f"{_fr(last['at'])} — {last['action']} : {last['result']}" if last else
                                    'Aucune action exécutée par la chaîne pour cette facture.', 'Journal de la chaîne')]))

    all_checks = [ch for s in steps[:7] for ch in s['checks']]
    level = V.level(all_checks)
    return {'purchase': p, 'supplier': sup, 'steps': steps, 'issues': issues, 'blocking': blocking, 'level': level,
            'actions': actions, 'proposed_entry': proposed, 'entry': entry, 'entry_lines': lines, 'treatment': treatment,
            'paid': round(paid, 2), 'due_left': due_left, 'log': log}


def queue(c, eid, today, days=120):
    """Factures fournisseurs à suivre : en attente de validation, non réglées, ou récentes."""
    since = (today - timedelta(days=days)).isoformat()
    rows = c.execute("""SELECT id FROM purchase_invoices WHERE entity_id IS ? AND COALESCE(validation_status,'approved')<>'rejected'
                        AND (COALESCE(validation_status,'approved')='pending' OR COALESCE(status,'unpaid')<>'paid'
                             OR COALESCE(issue_date,created_at)>=?) ORDER BY COALESCE(issue_date,created_at) DESC, id DESC LIMIT 300""",
                     (eid, since)).fetchall()
    from profitos.runtime import auth_cx
    ac = auth_cx()
    try:
        return [a for a in (analyse(c, eid, r['id'], today, ac) for r in rows) if a]
    finally:
        ac.close()


# ------------------------------------------------------------------------ exécution
def execute(c, eid, purchase_id, op, user, role, today):
    """Exécute une action AUTORISÉE après recalcul complet, puis contrôle le résultat sur les
    données. Retourne (ok, message). Chaque exécution est consignée au journal de la chaîne."""
    from profitos.accounting import AccountingError, generate_purchase_entry
    from profitos.runtime import now
    if role not in VALIDATOR_ROLES:
        return False, 'Seul un propriétaire, administrateur ou comptable peut valider une facture fournisseur.'
    a = analyse(c, eid, purchase_id, today)
    if not a:
        return False, 'Facture introuvable.'
    action = next((x for x in a['actions'] if x['op'] == op and x['kind'] == 'execute'), None)
    if not action:
        return False, "Cette action n'est plus proposée : la situation a changé."
    if not action['allowed']:
        why = '; '.join(f"{i['step']} : {i['label']}" for i in (a['blocking'] or a['issues']))
        return False, f"Action non lancée : {why}."
    try:
        if op == 'approve':
            c.execute("UPDATE purchase_invoices SET validation_status='approved',validated_by=?,validated_at=?,rejection_reason=NULL "
                      "WHERE id=? AND entity_id IS ? AND validation_status='pending'", (user, now(), purchase_id, eid))
        row = c.execute('SELECT * FROM purchase_invoices WHERE id=? AND entity_id IS ?', (purchase_id, eid)).fetchone()
        generate_purchase_entry(c, row, commit=False)
        c.commit()
    except AccountingError as exc:
        c.rollback()
        _log(c, eid, purchase_id, user, action['label'], False, f'Refusé par la comptabilité : {exc}')
        return False, f'Action annulée : {exc}'
    # Contrôle du résultat sur les données.
    after = analyse(c, eid, purchase_id, today)
    entry_ok = after['entry'] is not None and all(ch['ok'] for ch in after['steps'][4]['checks'])
    status_ok = (after['purchase'].get('validation_status') or 'approved') == 'approved'
    ok = entry_ok and status_ok
    result = (f"écriture du {_fr(after['entry']['entry_date'])} passée et équilibrée, facture validée" if ok else
              "contrôle en échec : " + ('écriture absente ou incohérente' if not entry_ok else 'statut non mis à jour'))
    _log(c, eid, purchase_id, user, action['label'], ok, result)
    return ok, f"{action['label']} : {result}."


# --------------------------------------------------------------------- Mission Control
def detect(c, eid, today):
    """Constat Mission Control : factures fournisseurs que la chaîne n'a pas pu mener au bout."""
    items = queue(c, eid, today)
    ready = [a for a in items if (a['purchase'].get('validation_status') == 'pending') and
             any(x['op'] == 'approve' and x['allowed'] for x in a['actions'])]
    to_check = [a for a in items if a['level'] == V.TO_CHECK and a not in ready]
    blocked = [a for a in items if a['level'] == V.BLOCKED]
    if not (ready or to_check or blocked):
        return []
    checks = [V.check('Chaîne de traitement exécutée', True, f"{len(items)} facture(s) fournisseur analysée(s) en dix étapes.",
                      'Chaîne de traitement')]
    if blocked:
        checks.append(V.check('Factures bloquées', False, f"{len(blocked)} facture(s) avec une anomalie bloquante (doublon, écart au "
                              "contrat, écriture incohérente…).", 'Chaîne de traitement',
                              fix='Ouvrez la chaîne de traitement : chaque facture indique l\'étape en cause et quoi faire.'))
    if to_check:
        checks.append(V.check('Factures à vérifier', None, f"{len(to_check)} facture(s) avec une information manquante ou incertaine "
                              "(justificatif, TVA, fiche fournisseur…).", 'Chaîne de traitement',
                              fix='Complétez les informations indiquées sur chaque facture.'))
    amount = round(sum(float(a['purchase']['total'] or 0) for a in ready + to_check + blocked), 2)
    kind = 'prepare' if ready else 'link'
    label = f"Valider {len(ready)} facture(s) prête(s)" if ready else 'Ouvrir la chaîne de traitement'
    return [V.finding('achats:chaine', 'mission_control', 'achat_traitement', 'important' if blocked else 'info',
                      f"Factures fournisseurs : {len(ready)} prête(s) à valider, {len(to_check)} à vérifier, {len(blocked)} bloquée(s)",
                      amount=amount, amount_label='Montant TTC des factures concernées', checks=checks,
                      action={'kind': kind, 'label': label, 'endpoint': 'purchase_pipeline', 'area': 'invoicing'},
                      follow_up_days=7, ref={'ready': len(ready), 'to_check': len(to_check), 'blocked': len(blocked)})]

"""Mission Control (v480) : détecter les problèmes, vérifier, préparer l'action, contrôler
le résultat. Chaque détecteur lit les données de ProfitOS et produit des constats au
format du moteur de vérification (profitos/verification.py).

Contrôles par type de constat :
- impayé : solde recalculé, aucun virement correspondant non rapproché sur un relevé
  bancaire à jour, e-mail client, pas de blocage de facturation électronique, délai
  minimal depuis la dernière relance → relance envoyée en un clic ;
- facture fournisseur due : validée, montant restant, trésorerie suffisante ;
- tension de trésorerie : prévision recalculée, solde de départ à jour ;
- mouvements bancaires non traités, échéances fiscales, pièces demandées, indicateurs de
  la veille suspendus.
"""
from datetime import date, timedelta

from profitos import verification as V

MODULE = 'mission_control'
BANK_FRESH_DAYS = 3          # relevé plus ancien : on ne peut pas affirmer qu'aucun paiement n'est arrivé
PAYMENT_MATCH_SCORE = 60     # score de rapprochement à partir duquel un virement « pourrait » régler la facture
PAYABLES_HORIZON = 7
BANK_PENDING_DAYS = 7

KIND_LABELS = {'contrat_preavis': 'Contrats fournisseurs', 'contrat_ecart': 'Contrats fournisseurs',
               'contrat_hors_periode': 'Contrats fournisseurs', 'hausse_sans_contrat': 'Hausses de prix',
               'doublon': 'Doublons possibles', 'dossier': 'Dossier entreprise', 'achat_traitement': 'Chaîne des achats',
               'impaye': 'Impayés clients', 'fournisseur': 'Paiements fournisseurs', 'tresorerie': 'Trésorerie',
               'banque': 'Banque', 'fiscal': 'Échéances fiscales', 'pieces': 'Pièces justificatives',
               'veille': 'Veille de votre secteur', 'fiscal_controle': 'Contrôles fiscaux', 'controle_indisponible': 'Contrôles'}


def _fr_money(x):
    from profitos.runtime import fr_number
    return f'{fr_number(x, 2)} €'


def _fr_date(value):
    if not value:
        return '—'
    d = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    return d.strftime('%d/%m/%Y')


def _d(value):
    try:
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------- banque
def bank_status(c, eid, today):
    """Comptes bancaires de l'entité et date de la donnée bancaire la plus récente, ou None."""
    accounts = c.execute("""SELECT provider, provider_account_id, last_synced_at, statement_balance_date FROM bank_accounts
                            WHERE entity_id IS ? AND COALESCE(disabled,0)=0 AND provider IN ('powens','swan','import')""",
                         (eid,)).fetchall()
    if not accounts:
        return None
    best = None
    for a in accounts:
        dates = [_d(a['last_synced_at']), _d(a['statement_balance_date'])]
        row = c.execute("SELECT MAX(transaction_date) d FROM bank_transactions WHERE provider=? AND provider_account_id=?",
                        (a['provider'], a['provider_account_id'])).fetchone()
        dates.append(_d(row['d'] if row else None))
        dates = [x for x in dates if x]
        if dates and (best is None or max(dates) > best):
            best = max(dates)
    return {'accounts': len(accounts), 'as_of': best,
            'fresh': bool(best) and (today - best).days <= BANK_FRESH_DAYS}


def _invoice_numbers(c, eid):
    """Numéros normalisés de toutes les factures émises de l'entité."""
    from profitos.routes.bank_sync import _norm_text
    return {r['id']: _norm_text(r['invoice_number'] or '') for r in
            c.execute('SELECT id, invoice_number FROM outgoing_invoices WHERE entity_id IS ?', (eid,)).fetchall()
            if (r['invoice_number'] or '').strip()}


def names_other_invoice(label, invoice_id, numbers):
    """Le libellé bancaire cite-t-il le numéro d'une AUTRE facture de l'entreprise ?
    Un tel virement ne peut pas être proposé pour celle-ci."""
    from profitos.routes.bank_sync import _norm_text
    low = _norm_text(label or '')
    own = numbers.get(invoice_id)
    return any(n and n in low and n != own and not (own and n in own) for i, n in numbers.items() if i != invoice_id)


def probable_bank_payment(c, inv, balance):
    """Virement reçu, non encore rapproché, qui pourrait régler la facture (ou None).
    Réutilise le score de rapprochement de la banque (montant, n° de facture, client)."""
    from profitos.bank_reconciliation import _tx_filter
    from profitos.routes.bank_sync import _bank_allocated_total, _bank_match_score
    since = inv['issue_date'] or '0000-01-01'
    txs = c.execute(f"""SELECT t.* FROM bank_transactions t
                        JOIN bank_accounts a ON a.provider_account_id=t.provider_account_id AND a.provider=t.provider
                        WHERE a.entity_id IS ? AND t.amount>0 AND COALESCE(t.transaction_date,'')>=? AND {_tx_filter()}
                        ORDER BY t.transaction_date DESC, t.id DESC LIMIT 300""", (inv['entity_id'], since)).fetchall()
    numbers = _invoice_numbers(c, inv['entity_id'])
    best = None
    for t in txs:
        available = round(float(t['amount'] or 0) - _bank_allocated_total(c, t['id'], inv['entity_id']), 2)
        if available <= 0.005 or names_other_invoice(t['label'], inv['id'], numbers):
            continue
        score, reasons = _bank_match_score(inv, t, balance, available)
        if score >= PAYMENT_MATCH_SCORE and (best is None or score > best[1]):
            best = (t, score, reasons)
    return best


def _bank_check(c, inv, balance, bank, today):
    """Contrôle « aucun paiement déjà arrivé » et le virement candidat éventuel."""
    label = 'Aucun paiement déjà reçu en banque'
    if bank is None:
        return V.check(label, None, "Aucun compte bancaire relié : impossible de savoir si le client a déjà payé.", 'Banque',
                       fix="Connectez la banque ou importez un relevé récent (menu Banque)."), None
    if not bank['fresh']:
        when = _fr_date(bank['as_of']) if bank['as_of'] else 'date inconnue'
        return V.check(label, None, f"Dernier relevé bancaire : {when} (plus de {BANK_FRESH_DAYS} jours) : un paiement récent "
                       "pourrait manquer.", 'Banque',
                       fix="Synchronisez la banque ou importez un relevé de moins de 3 jours."), None
    candidate = probable_bank_payment(c, inv, balance)
    if candidate:
        t, score, reasons = candidate
        return V.check(label, None,
                       f"Un virement de {_fr_money(t['amount'])} du {_fr_date(t['transaction_date'])} "
                       f"(« {(t['label'] or '')[:60]} », {', '.join(reasons)}) pourrait régler cette facture.", 'Banque',
                       fix="S'il correspond, rapprochez-le (la facture sera soldée) ; sinon, écartez la proposition "
                           "sur la page Banque et la relance redeviendra possible."), t
    return V.check(label, True,
                   f"Aucun virement correspondant non rapproché sur les relevés à jour du {_fr_date(bank['as_of'])}.",
                   'Banque'), None


# ------------------------------------------------------------ décisions sur les créances
OLD_RECEIVABLE_DAYS = 180
DECISIONS = {'due': 'Toujours due', 'litige': 'En litige', 'douteuse': 'Douteuse (recouvrement incertain)'}


def ensure_decisions(c):
    c.execute('''CREATE TABLE IF NOT EXISTS receivable_decisions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id INTEGER, source TEXT NOT NULL, invoice_id INTEGER NOT NULL,
        status TEXT NOT NULL, note TEXT, decided_at TEXT, decided_by TEXT)''')
    c.commit()


def decision(c, eid, source, invoice_id):
    ensure_decisions(c)
    r = c.execute('SELECT * FROM receivable_decisions WHERE entity_id IS ? AND source=? AND invoice_id=?',
                  (eid, source, invoice_id)).fetchone()
    return dict(r) if r else None


def set_decision(c, eid, source, invoice_id, status, note, user, today):
    if status not in DECISIONS and status != 'aucune':
        raise ValueError('Décision inconnue.')
    ensure_decisions(c)
    # Remplacement explicite : entity_id vaut NULL pour la société mère, et NULL ne déclenche
    # jamais une contrainte d'unicité (une seconde décision s'ajouterait au lieu de remplacer).
    c.execute('DELETE FROM receivable_decisions WHERE entity_id IS ? AND source=? AND invoice_id=?', (eid, source, invoice_id))
    if status != 'aucune':
        c.execute("""INSERT INTO receivable_decisions(entity_id,source,invoice_id,status,note,decided_at,decided_by)
                     VALUES(?,?,?,?,?,?,?)""",
                  (eid, source, invoice_id, status, (note or '')[:300] or None, today.isoformat(), user))
    c.commit()


def reminder_hold(c, inv):
    """Raison de ne pas relancer une facture émise (litige ou créance douteuse déclarés), ou None."""
    try:
        d = decision(c, inv['entity_id'], 'outgoing', inv['id'])
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        return None
    if d and d['status'] in ('litige', 'douteuse'):
        return (f"Créance déclarée {DECISIONS[d['status']].lower()} le {_fr_date(d['decided_at'])}"
                + (f" ({d['note']})" if d.get('note') else '') + ' : relance suspendue.')
    return None


# ----------------------------------------------------------------------- détecteurs
def detect_receivables(c, eid, today):
    from profitos import invoice_payment as IP
    from profitos.receivables import detail_url, open_receivables
    bank = bank_status(c, eid, today)
    auto = IP.load(c, eid)
    out = []
    for r in open_receivables(c, eid, today):
        if not r['overdue']:
            continue
        outgoing = r['source'] == 'outgoing'
        inv = c.execute('SELECT * FROM outgoing_invoices WHERE id=? AND entity_id IS ?', (r['id'], eid)).fetchone() \
            if outgoing else None
        probe = inv or {'id': r['id'], 'entity_id': eid, 'issue_date': r['issue_date'],
                        'invoice_number': r['invoice_number'], 'client_name': r['customer']}
        dec = decision(c, eid, r['source'], r['id'])
        checks = [V.check('Solde restant dû recalculé', True,
                          f"{_fr_money(r['outstanding'])} sur {_fr_money(r['total'])} (règlements et avoirs déduits).",
                          'Factures et règlements ProfitOS')]
        bank_c, candidate = _bank_check(c, probe, r['outstanding'], bank, today)
        checks.append(bank_c)
        # Statut de la créance : une décision est demandée au-delà de 6 mois, et un litige bloque la relance.
        if dec and dec['status'] in ('litige', 'douteuse'):
            checks.append(V.check('Statut de la créance', False,
                                  f"Déclarée {DECISIONS[dec['status']].lower()} le {_fr_date(dec['decided_at'])} par "
                                  f"{dec['decided_by']}" + (f" : {dec['note']}" if dec.get('note') else '') + '.',
                                  'Décision enregistrée',
                                  fix=("Quand le litige est réglé, choisissez « Toujours due » pour reprendre les relances."
                                       if dec['status'] == 'litige' else
                                       "Voyez avec votre expert-comptable la provision pour créance douteuse ; une mise en "
                                       "demeure reste possible (Dossier & démarches).")))
        elif r['days_overdue'] > OLD_RECEIVABLE_DAYS and not (dec and dec['status'] == 'due' and
                                                               (today - date.fromisoformat(dec['decided_at'])).days <= OLD_RECEIVABLE_DAYS):
            checks.append(V.check('Statut de la créance confirmé', None,
                                  f"Impayée depuis {r['days_overdue']} jours : au-delà de 6 mois, une relance simple ne suffit "
                                  "plus et la facture peut être contestée, déjà réglée autrement ou difficile à recouvrer.",
                                  'Calcul ProfitOS',
                                  fix="Indiquez ci-dessous si elle est toujours due, en litige ou douteuse ; si elle a été "
                                      "payée hors banque, enregistrez le règlement sur la facture."))
        elif dec and dec['status'] == 'due':
            checks.append(V.check('Statut de la créance confirmé', True,
                                  f"Toujours due, confirmé le {_fr_date(dec['decided_at'])} par {dec['decided_by']}.",
                                  'Décision enregistrée'))
        evidence = [V.evidence('Échéance', _fr_date(r['due_date']), f"Facture {r['invoice_number'] or ''}".strip()),
                    V.evidence('Retard', f"{r['days_overdue']} jour(s)", 'Calcul ProfitOS', today.isoformat())]
        action, alternatives = None, []
        if candidate:
            alternatives.append({'label': 'Rapprocher le virement', 'endpoint': 'mission_control_bank'})
        if outgoing:
            prev = c.execute("SELECT COUNT(*) n, MAX(sent_at) last FROM invoice_reminders WHERE invoice_id=? AND entity_id IS ?",
                             (r['id'], eid)).fetchone()
            n, last = int(prev['n'] or 0), _d(prev['last'])
            checks.append(V.check('E-mail du client renseigné', bool(inv['client_email']),
                                  inv['client_email'] or 'Aucun e-mail sur la facture.', 'Facture',
                                  fix=None if inv['client_email'] else "Ajoutez l'e-mail du client sur la facture."))
            hold = r.get('einvoice_hold')
            checks.append(V.check('Pas de blocage de facturation électronique', not hold,
                                  f"Facture {hold} côté facturation électronique." if hold else 'Aucun blocage.',
                                  'Facturation électronique',
                                  fix="Régularisez la facture côté facturation électronique." if hold else None))
            if last and (today - last).days < IP.MIN_GAP_DAYS:
                nxt = last + timedelta(days=IP.MIN_GAP_DAYS)
                checks.append(V.check(f'Au moins {IP.MIN_GAP_DAYS} jours depuis la dernière relance', False,
                                      f"Dernière relance le {_fr_date(last)}.", 'Relances envoyées',
                                      fix=f"Attendez le {_fr_date(nxt)} pour la relance suivante."))
            else:
                checks.append(V.check(f'Au moins {IP.MIN_GAP_DAYS} jours depuis la dernière relance', True,
                                      f"Dernière relance le {_fr_date(last)}." if last else 'Aucune relance envoyée.',
                                      'Relances envoyées'))
            evidence.append(V.evidence('Relances envoyées', str(n), 'Historique des relances'))
            auto_on = auto['auto_reminders'] and r['id'] not in auto['excluded']
            auto_label = 'actives' if auto_on else ('désactivées pour cette facture' if auto['auto_reminders'] else 'désactivées')
            evidence.append(V.evidence('Relances automatiques', auto_label, 'Encaissement et relances'))
            action = {'kind': 'execute', 'op': 'send_reminder', 'label': f'Envoyer la relance n°{n + 1}',
                      'area': 'invoicing', 'invoice_id': r['id']}
            alternatives.append({'label': 'Ouvrir la facture', 'endpoint': 'invoicing_detail', 'args': {'invoice_id': r['id']}})
        else:
            action = {'kind': 'link', 'label': 'Ouvrir la créance', 'url': detail_url(r)}
        severity = 'info' if dec and dec['status'] == 'douteuse' else ('critique' if r['days_overdue'] > 30 else 'important')
        f = V.finding(f"impaye:{r['source']}:{r['id']}", MODULE, 'impaye', severity,
                      f"Facture {r['invoice_number'] or 'sans numéro'} — {r['customer']} : impayée depuis "
                      f"{r['days_overdue']} jour(s)", amount=r['outstanding'], checks=checks, evidence=evidence,
                      action=action, alternatives=alternatives, follow_up_days=7,
                      ref={'source': r['source'], 'id': r['id'], 'number': r['invoice_number']})
        f['decision'] = {'source': r['source'], 'invoice_id': r['id'], 'current': dec['status'] if dec else None,
                         'asked': r['days_overdue'] > OLD_RECEIVABLE_DAYS or bool(dec)}
        out.append(f)
    return out


def detect_payables(c, eid, today):
    from profitos import treasury as T
    rows = c.execute("""SELECT p.*, COALESCE((SELECT SUM(x.amount) FROM purchase_invoice_payments x
                                               WHERE x.purchase_invoice_id=p.id), 0) AS paid
                        FROM purchase_invoices p
                        WHERE p.entity_id IS ? AND COALESCE(p.status,'unpaid') NOT IN ('paid','cancelled')
                          AND COALESCE(p.validation_status,'approved')<>'rejected'
                          AND p.due_date IS NOT NULL AND p.due_date<=? ORDER BY p.due_date, p.id""",
                     (eid, (today + timedelta(days=PAYABLES_HORIZON)).isoformat())).fetchall()
    bank = None
    try:
        bank = T.latest_bank_balance(c, eid)
    except Exception:
        bank = None
    out, running = [], 0.0
    for p in rows:
        due_amount = round(max(0.0, float(p['total'] or 0) - float(p['paid'] or 0)), 2)
        if due_amount <= 0.005:
            continue
        running = round(running + due_amount, 2)
        status = (p['validation_status'] or 'approved')
        checks = [V.check('Facture validée', status == 'approved',
                          'Validée.' if status == 'approved' else 'En attente de validation interne.', 'Achats'),
                  V.check('Montant restant recalculé', True, f"{_fr_money(due_amount)} sur {_fr_money(p['total'])}.",
                          'Factures et règlements fournisseurs')]
        if bank is None:
            checks.append(V.check('Trésorerie suffisante', None, 'Solde bancaire inconnu : reliez la banque ou importez un relevé.',
                                  'Banque'))
        elif (today - date.fromisoformat(bank['as_of'])).days > BANK_FRESH_DAYS:
            checks.append(V.check('Trésorerie suffisante', None,
                                  f"Solde de {_fr_money(bank['balance'])} au {_fr_date(bank['as_of'])} : trop ancien pour conclure.",
                                  'Banque'))
        else:
            enough = bank['balance'] >= running
            checks.append(V.check('Trésorerie suffisante', enough,
                                  f"Solde de {_fr_money(bank['balance'])} au {_fr_date(bank['as_of'])} pour "
                                  f"{_fr_money(running)} de paiements dus jusqu'à celui-ci." +
                                  ('' if enough else ' Priorisez ou négociez un délai.'), 'Banque'))
        due = date.fromisoformat(p['due_date'][:10])
        late = (today - due).days
        title = (f"Facture fournisseur {p['invoice_number']} — {p['supplier_name']} : "
                 + (f"échue depuis {late} jour(s)" if late > 0 else ("due aujourd'hui" if late == 0 else f"due le {_fr_date(due)}")))
        out.append(V.finding(f"fournisseur:{p['id']}", MODULE, 'fournisseur', 'important' if late > 0 else 'info', title,
                             amount=due_amount, checks=checks,
                             evidence=[V.evidence('Échéance', _fr_date(due), f"Facture {p['invoice_number']}")],
                             action={'kind': 'link', 'label': 'Préparer le virement', 'endpoint': 'purchase_sepa_batch'},
                             alternatives=[{'label': 'Ouvrir la facture', 'endpoint': 'purchase_detail',
                                            'args': {'purchase_id': p['id']}}],
                             follow_up_days=3, ref={'id': p['id'], 'number': p['invoice_number']}))
    return out


def detect_cash(cash, today):
    if not cash:
        return []
    lvl = cash.get('alert_level')
    if lvl == 'INCOMPLET':
        return [V.finding('tresorerie:incomplet', MODULE, 'tresorerie', 'info', 'Prévision de trésorerie incomplète',
                          checks=[V.check('Solde bancaire de départ connu', None, cash.get('alert') or '', 'Trésorerie')],
                          action={'kind': 'link', 'label': 'Renseigner le solde', 'endpoint': 'cash_intelligence'},
                          follow_up_days=3)]
    if lvl not in ('ALERTE', 'VIGILANCE'):
        return []
    checks = [V.check('Prévision recalculée sur les données du jour', True, cash.get('alert') or '', 'Trésorerie & scénarios'),
              V.check('Solde de départ à jour', not cash.get('balance_stale') or None,
                      f"Solde du {_fr_date(cash.get('cash_as_of'))}" +
                      (f" ({cash.get('balance_age_days')} jours) : mettez-le à jour." if cash.get('balance_stale') else '.'),
                      'Trésorerie & scénarios'),
              V.check('Rythme de dépenses estimé sur assez de données', True if cash.get('burn_reliable') else None,
                      f"{cash.get('expense_rows', 0)} dépense(s) récente(s) prises en compte.", 'Dépenses')]
    evidence = []
    if cash.get('min_cash') is not None:
        when = (today + timedelta(days=cash['min_day'])).isoformat() if cash.get('min_day') is not None else None
        evidence.append(V.evidence('Point bas prévu', _fr_money(cash['min_cash']), 'Prévision 90 jours', when))
    if cash.get('cash_balance') is not None:
        evidence.append(V.evidence('Solde de départ', _fr_money(cash['cash_balance']), 'Trésorerie', str(cash.get('cash_as_of') or '')[:10]))
    title = 'Tension de trésorerie prévue' if lvl == 'ALERTE' else 'Trésorerie sous surveillance'
    return [V.finding('tresorerie:tension', MODULE, 'tresorerie', 'critique' if lvl == 'ALERTE' else 'important', title,
                      amount=cash.get('min_cash'), checks=checks, evidence=evidence,
                      action={'kind': 'link', 'label': 'Ouvrir le plan de trésorerie', 'endpoint': 'cash_intelligence'},
                      alternatives=[{'label': 'Simuler une décision', 'endpoint': 'decision_simulator'}],
                      follow_up_days=7)]


def pending_transactions(c, eid, today):
    """Mouvements bancaires des 90 derniers jours, vieux de plus de 7 jours, ni rapprochés, ni
    comptabilisés, ni ignorés."""
    from profitos.bank_reconciliation import _tx_filter
    limit = (today - timedelta(days=BANK_PENDING_DAYS)).isoformat()
    since = (today - timedelta(days=90)).isoformat()
    base = f"""FROM bank_transactions t
               JOIN bank_accounts a ON a.provider_account_id=t.provider_account_id AND a.provider=t.provider
               WHERE a.entity_id IS ? AND {_tx_filter()} AND t.transaction_date>=? AND t.transaction_date<=?
                 AND NOT EXISTS (SELECT 1 FROM bank_invoice_allocations x WHERE x.entity_id IS ? AND x.bank_transaction_id=t.id)
                 AND NOT EXISTS (SELECT 1 FROM bank_purchase_allocations y WHERE y.entity_id IS ? AND y.bank_transaction_id=t.id)
                 AND NOT EXISTS (SELECT 1 FROM accounting_entries e WHERE e.entity_id IS ?
                                 AND e.source_type IN ('bank_fee','bank_categorization') AND e.source_id=t.id)"""
    params = [eid, since, limit, eid, eid, eid]
    try:
        rows = c.execute(f"""SELECT t.*, a.entity_id AS entity_id {base}
                             AND NOT EXISTS (SELECT 1 FROM bank_transaction_workflow w WHERE w.entity_id IS ?
                                             AND w.bank_transaction_id=t.id)
                             ORDER BY t.transaction_date, t.id""", (*params, eid)).fetchall()
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        rows = c.execute(f'SELECT t.*, a.entity_id AS entity_id {base} ORDER BY t.transaction_date, t.id', params).fetchall()
    return [dict(r) for r in rows]


PROPOSAL_MIN_SCORE = 60        # même seuil que pour bloquer une relance
PROPOSAL_AMBIGUITY = 15        # écart minimal avec la 2e meilleure correspondance (sinon : ambigu)


def bank_proposals(c, eid, today):
    """Propositions de rapprochement pour les mouvements en attente, chacune avec ses preuves.
    Retourne (propositions, non_rapprochables) ; une proposition = {'id', 'tx', 'target_kind',
    'target', 'amount', 'score', 'confidence', 'reasons'}. Jamais exécutées sans validation."""
    from profitos.routes.bank_sync import (_bank_allocated_total, _bank_match_score, _invoice_bank_balance,
                                           _purchase_reconciliation_suggestions)
    txs = pending_transactions(c, eid, today)
    invoices = c.execute("SELECT * FROM outgoing_invoices WHERE entity_id IS ? AND status IN ('sent','partially_paid') AND total>0 "
                         "ORDER BY issue_date, id", (eid,)).fetchall()
    numbers = _invoice_numbers(c, eid)
    proposals, unmatched = [], []
    for t in txs:
        amount = float(t['amount'] or 0)
        cands = []
        if amount > 0:
            available = round(amount - _bank_allocated_total(c, t['id'], eid), 2)
            for inv in invoices:
                balance = _invoice_bank_balance(c, inv)
                if balance <= .005 or available <= .005:
                    continue
                if names_other_invoice(t['label'], inv['id'], numbers):
                    continue
                score, reasons = _bank_match_score(inv, t, balance, available)
                if score >= 30:
                    cands.append((score, 'facture', inv, min(balance, available), reasons))
        elif amount < 0:
            for sug in _purchase_reconciliation_suggestions(c, t):
                cands.append((sug['score'], 'achat', sug['purchase'], sug['amount'], sug['reasons'].split(', ')))
        cands.sort(key=lambda x: -x[0])
        if not cands or cands[0][0] < PROPOSAL_MIN_SCORE:
            unmatched.append({'tx': t, 'reason': ("Aucune facture client ouverte ne correspond (montant, n° de facture, client)."
                                                  if amount > 0 else
                                                  "Aucune facture fournisseur ouverte ne correspond : frais, abonnement ou "
                                                  "dépense à comptabiliser.")})
            continue
        if len(cands) > 1 and cands[0][0] < 80 and cands[0][0] - cands[1][0] < PROPOSAL_AMBIGUITY:
            unmatched.append({'tx': t, 'reason': f"Plusieurs factures possibles ({cands[0][2]['invoice_number']}, "
                                                 f"{cands[1][2]['invoice_number']}…) : choix à faire sur la page Banque."})
            continue
        score, kind, target, alloc, reasons = cands[0]
        proposals.append({'id': f"{t['id']}:{kind}:{target['id']}", 'tx': t, 'target_kind': kind, 'target': dict(target),
                          'amount': round(alloc, 2), 'score': score, 'reasons': list(reasons),
                          'confidence': 'Élevée' if score >= 90 else ('Moyenne' if score >= 75 else 'Faible')})
    # Une même facture ne peut être proposée que pour un seul mouvement : la meilleure correspondance.
    best = {}
    for p in proposals:
        k = (p['target_kind'], p['target']['id'])
        if k not in best or p['score'] > best[k]['score']:
            best[k] = p
    kept = [p for p in proposals if best[(p['target_kind'], p['target']['id'])] is p]
    for p in proposals:
        if p not in kept:
            unmatched.append({'tx': p['tx'], 'reason': f"Correspond aussi à la facture {p['target']['invoice_number']}, déjà "
                                                       "proposée pour un autre mouvement : à vérifier sur la page Banque."})
    return kept, unmatched


def apply_bank_proposals(c, eid, today, selected_ids):
    """Exécute les rapprochements choisis APRÈS les avoir recalculés : un rapprochement qui
    n'est plus proposé (situation changée) n'est pas exécuté. Retourne (faits, refusés)."""
    from profitos.routes.bank_sync import reconcile_invoice, reconcile_purchase
    proposals, _u = bank_proposals(c, eid, today)
    by_id = {p['id']: p for p in proposals}
    done, refused = [], []
    for pid in selected_ids:
        p = by_id.get(pid)
        if not p:
            refused.append((pid, "n'est plus proposé (déjà traité ou situation changée)"))
            continue
        try:
            if p['target_kind'] == 'facture':
                amount, _inv = reconcile_invoice(c, eid, p['tx']['id'], p['target']['id'], p['amount'])
            else:
                amount, _p = reconcile_purchase(c, eid, p['tx']['id'], p['target']['id'], p['amount'])
            done.append((p, amount))
        except Exception as exc:                        # chaque rapprochement est annulé seul en cas d'erreur
            refused.append((pid, str(exc) or type(exc).__name__))
    return done, refused


def detect_bank_pending(c, eid, today):
    txs = pending_transactions(c, eid, today)
    if not txs:
        return []
    n = len(txs)
    volume = round(sum(abs(float(t['amount'] or 0)) for t in txs), 2)
    credits = sum(1 for t in txs if float(t['amount'] or 0) > 0)
    proposals, unmatched = bank_proposals(c, eid, today)
    checks = [V.check('Mouvements comptés sur les relevés', True,
                      f"{n} mouvement(s) ({credits} entrée(s), {n - credits} sortie(s)) ni rapprochés, ni comptabilisés, ni ignorés.",
                      'Banque')]
    if proposals:
        checks.append(V.check('Correspondances vérifiées', True,
                              f"{len(proposals)} rapprochement(s) proposé(s) avec preuves (montant, n° de facture, nom) ; "
                              f"{len(unmatched)} mouvement(s) sans correspondance sûre.", 'Calcul ProfitOS'))
        action = {'kind': 'prepare', 'label': f'Examiner {len(proposals)} proposition(s) de rapprochement',
                  'endpoint': 'mission_control_bank', 'area': 'banking'}
    else:
        checks.append(V.check('Correspondances vérifiées', None,
                              "Aucun mouvement ne correspond de façon sûre à une facture ouverte.", 'Calcul ProfitOS',
                              fix="Comptabilisez, rapprochez ou ignorez ces mouvements sur la page Banque."))
        action = {'kind': 'link', 'label': 'Traiter les mouvements', 'endpoint': 'banking'}
    return [V.finding('banque:a_traiter', MODULE, 'banque', 'important' if n >= 20 else 'info',
                      f'{n} mouvement(s) bancaire(s) non traité(s) depuis plus de {BANK_PENDING_DAYS} jours',
                      amount=volume, checks=checks, action=action,
                      alternatives=[{'label': 'Ouvrir la page Banque', 'endpoint': 'banking'}] if proposals else [],
                      detail="Le montant est le volume cumulé des mouvements à vérifier (entrées et sorties) : ce n'est ni "
                             "une perte, ni une somme à récupérer.",
                      follow_up_days=7, ref={'count': n, 'proposals': len(proposals)})]


def detect_fiscal(c, eid, today):
    from profitos import automation as A
    out = []
    for it in A.overdue_declarations(c, eid, today):
        key = f"fiscal:{it.get('type') or it.get('form')}:{it['date']}"
        out.append(V.finding(key, MODULE, 'fiscal', 'critique',
                             f"{it['label']} : en retard de {it['days_late']} jour(s)", amount=it.get('amount'),
                             checks=[V.check('Échéance passée sans dépôt enregistré', True,
                                             f"Échéance du {_fr_date(it['date'])}, aucun dépôt ni justificatif archivé.",
                                             'Échéancier fiscal')],
                             evidence=[V.evidence('Formulaire', it.get('form') or '—', 'Échéancier fiscal')],
                             action={'kind': 'link', 'label': 'Préparer la déclaration', 'endpoint': 'teledeclarations'},
                             alternatives=[{'label': "Voir l'échéancier", 'endpoint': 'fiscal_calendar_page'}],
                             follow_up_days=3, ref={'type': it.get('type'), 'date': str(it['date'])}))
    for it in A.upcoming_deadlines(c, eid, today, horizon_days=7):
        if it.get('deposited') or it['date'] < today or (it.get('type') == 'TVA_CA3' and not it.get('amount')):
            continue
        key = f"fiscal:{it.get('type') or it.get('form')}:{it['date']}"
        out.append(V.finding(key, MODULE, 'fiscal', 'important', f"{it['label']} : à déposer avant le {_fr_date(it['date'])}",
                             amount=it.get('amount'),
                             checks=[V.check('Échéance dans les 7 jours, non déposée', True, '', 'Échéancier fiscal')],
                             action={'kind': 'link', 'label': 'Préparer la déclaration', 'endpoint': 'teledeclarations'},
                             follow_up_days=max(1, (it['date'] - today).days), ref={'type': it.get('type'), 'date': str(it['date'])}))
    return out


def detect_documents(c, eid, today):
    from profitos import document_requests as DR
    rows = DR.open_requests(c, eid)
    if not rows:
        return []
    oldest = min(str(r['created_at'])[:10] for r in rows)
    return [V.finding('pieces:demandees', MODULE, 'pieces', 'important' if len(rows) >= 5 else 'info',
                      f'{len(rows)} pièce(s) justificative(s) demandée(s) par votre expert-comptable',
                      checks=[V.check('Demandes ouvertes', True, f'La plus ancienne date du {_fr_date(oldest)}.',
                                      'Pièces demandées')],
                      action={'kind': 'link', 'label': 'Joindre les pièces', 'endpoint': 'accounting_document_requests'},
                      follow_up_days=7)]


def detect_watch(c, eid, today):
    from profitos import market_watch as MW
    from profitos.runtime import auth_cx
    settings = MW.load_settings(c, eid)
    if not settings.get('sector') and settings.get('series') is None and not settings.get('custom'):
        return []
    ac = auth_cx()
    try:
        out = []
        for code in MW.series_for(settings):
            meta = MW.series_meta(ac, code) or {}
            if meta.get('status') != 'mismatch':
                continue
            defn = MW.definition(code, settings.get('custom')) or {'label': code}
            out.append(V.finding(f'veille:suspendu:{code}', MODULE, 'veille', 'info',
                                 f"Indicateur suspendu : {defn['label']}",
                                 checks=[V.check('Métadonnées officielles cohérentes', False, meta.get('error') or '', 'INSEE')],
                                 action={'kind': 'link', 'label': 'Voir la série', 'endpoint': 'market_watch_series',
                                         'args': {'code': code}}, follow_up_days=30))
        return out
    finally:
        ac.close()


def detect_contracts(c, eid, today):
    from profitos import contract_guardian as CG
    return CG.detect(c, eid, today)


def _contract_kinds():
    from profitos import contract_guardian as CG
    return CG.KINDS


def detect_purchase_pipeline(c, eid, today):
    from profitos import purchase_pipeline as PP
    return PP.detect(c, eid, today)


def detect_fiscal_controls(c, eid, today):
    from profitos import fiscal_controls as FC
    return FC.detect(c, eid, today)


def detect_dossier(c, eid, today):
    from profitos import once_done as OD
    return OD.detect(c, eid, today)


# (types contrôlés, nom, fonction) : un détecteur peut produire plusieurs types de constats.
DETECTORS = [('impaye', 'impayés clients', detect_receivables), ('fournisseur', 'paiements fournisseurs', detect_payables),
             ('banque', 'mouvements bancaires', detect_bank_pending), ('fiscal', 'échéances fiscales', detect_fiscal),
             ('pieces', 'pièces demandées', detect_documents), ('veille', 'veille de secteur', detect_watch),
             (_contract_kinds, 'contrats fournisseurs', detect_contracts), ('dossier', 'dossier entreprise', detect_dossier),
             ('achat_traitement', 'chaîne des achats', detect_purchase_pipeline),
             ('fiscal_controle', 'contrôles fiscaux', detect_fiscal_controls)]


def compute(c, eid, today, cash=None, accounting=True):
    """Tous les constats du jour et les types effectivement contrôlés. cash : résultat de la
    prévision de trésorerie (None hors session web : la trésorerie n'est alors pas contrôlée)."""
    findings, evaluated = [], set()
    for kind, name, fn in DETECTORS:
        if kind in ('fiscal', 'pieces', 'fiscal_controle') and not accounting:
            continue
        try:
            findings += fn(c, eid, today)
            evaluated.update(kind() if callable(kind) else (kind,))
        except Exception as exc:
            try:
                c.rollback()
            except Exception:
                pass
            findings.append(V.unavailable(MODULE, name, exc))
    if cash is not None:
        findings += detect_cash(cash, today)
        evaluated.add('tresorerie')
    evaluated.add('controle_indisponible')
    return findings, evaluated


# ------------------------------------------------------------- contrôle des résultats
def resolve_note(c, eid, today):
    """Explication factuelle de la disparition d'un constat (pour le suivi)."""
    def note(item):
        ref, kind = item.get('ref') or {}, item.get('kind')
        if kind == 'impaye' and ref.get('source') == 'outgoing':
            inv = c.execute('SELECT status, paid_at FROM outgoing_invoices WHERE id=? AND entity_id IS ?', (ref['id'], eid)).fetchone()
            last = c.execute('SELECT MAX(payment_date) d FROM outgoing_invoice_payments WHERE invoice_id=? AND entity_id IS ?',
                             (ref['id'], eid)).fetchone()
            if inv and (inv['status'] == 'paid' or last['d']):
                return f"Facture réglée (dernier règlement enregistré le {_fr_date(last['d'] or inv['paid_at'])})."
            if inv and inv['status'] == 'cancelled':
                return 'Facture annulée.'
        if kind == 'fournisseur':
            p = c.execute('SELECT status, paid_at FROM purchase_invoices WHERE id=? AND entity_id IS ?', (ref.get('id'), eid)).fetchone()
            if p and p['status'] == 'paid':
                return f"Facture fournisseur réglée le {_fr_date(p['paid_at'])}."
        if kind == 'fiscal':
            return f"Déclaration déposée ou échéance traitée (contrôle du {_fr_date(today)})."
        if kind == 'tresorerie':
            return f"La prévision du {_fr_date(today)} ne montre plus de tension."
        if kind == 'banque':
            return 'Mouvements rapprochés, comptabilisés ou ignorés.'
        if kind == 'pieces':
            return 'Pièces jointes : plus aucune demande ouverte.'
        return None
    return note


def run(c, eid, today, cash=None, accounting=True):
    findings, evaluated = compute(c, eid, today, cash, accounting)
    return V.sync(c, eid, findings, today, resolve_note(c, eid, today), evaluated, owner=MODULE)


# --------------------------------------------------------------------- actions
def execute(c, eid, f, org):
    """Exécute l'action d'un constat RECALCULÉ et vérifié. Retourne (ok, message)."""
    if not V.can_execute(f):
        return False, "Action impossible : toutes les vérifications ne sont pas validées."
    op = f['action']['op']
    if op == 'send_reminder':
        from profitos.routes.invoicing import reminder_blocker, send_invoice_reminder
        inv = c.execute('SELECT * FROM outgoing_invoices WHERE id=? AND entity_id IS ?', (f['action']['invoice_id'], eid)).fetchone()
        if not inv:
            return False, 'Facture introuvable.'
        blocker = reminder_blocker(c, inv)
        if blocker:
            return False, blocker
        result = send_invoice_reminder(c, inv, org)
        if result.get('dry_run'):
            return False, (f"Service e-mail non configuré — relance non envoyée réellement (mode simulation) à "
                           f"{inv['client_email']}.")
        if result.get('sent'):
            return True, f"Relance n°{result.get('reminder_number')} envoyée à {inv['client_email']}"
        return False, "La relance n'a pas pu être envoyée. Réessayez dans quelques minutes."
    return False, 'Action inconnue.'

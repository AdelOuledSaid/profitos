"""Contrôles fiscaux croisés et dossier de validation pour l'expert-comptable (v490).

Complète les pré-contrôles de clôture (automation.precontrols) par des rapprochements entre
ce qui a été déclaré et ce que dit la comptabilité aujourd'hui :

- TVA : déclarations CA3 préparées ou déposées comparées aux écritures actuelles de la même
  période (une écriture modifiée après dépôt appelle une régularisation) ; cohérence base ×
  taux ; TVA non ventilée ; périodes non liquidées ; TVA à décaisser restée impayée ;
  chiffre d'affaires comptable rapproché de la base taxée ;
- IS : acomptes versés comparés aux acomptes exigibles ; excédent d'acomptes à récupérer ;
- liasse : équilibre du bilan et comptes rattachés par défaut ;
- clôture : pré-contrôles existants.

Chaque contrôle suit le moteur de vérification (ok True / None à vérifier / False bloquant),
cite sa source et dit quoi faire. ProfitOS **prépare** ; la transmission officielle se fait
par un partenaire EDI agréé ou par l'entreprise sur impots.gouv.fr, et seul un accusé EDI
constate l'acceptation (voir docs/validation-fiscale.md).
"""
import json
from datetime import date, timedelta

from profitos import verification as V

MODULE = 'mission_control'   # constats suivis et soldés par Mission Control
KIND = 'fiscal_controle'
CA3_COMPARED = (('16', 'TVA brute due'), ('19', 'TVA déductible sur immobilisations'),
                ('20', 'TVA déductible sur autres biens et services'))
FILED = ('deposited', 'acknowledged', 'accepted', 'declared', 'to_verify', 'confirmed', 'filed')
INSTALMENT_GRACE_DAYS = 5


def _r(x):
    return round(float(x or 0), 2)


def _eur(x):
    from profitos.runtime import fr_number
    return f'{fr_number(x, 2)} €'


def _fr(d):
    d = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
    return d.strftime('%d/%m/%Y')


def _ck(code, label, ok, detail='', source=None, fix=None, amount=None):
    c = V.check(label, ok, detail, source, fix)
    c['code'] = code
    c['amount'] = amount
    return c


def _ef(eid):
    return ('e.entity_id=?', (eid,)) if eid else ('e.entity_id IS NULL', ())


def _turnover(c, eid, start, end):
    ef, ep = _ef(eid)
    row = c.execute(f"""SELECT COALESCE(SUM(l.credit-l.debit),0) n FROM accounting_entry_lines l
                        JOIN accounting_entries e ON e.id=l.entry_id
                        WHERE l.account_code LIKE ? AND e.journal_code<>'AN' AND e.entry_date BETWEEN ? AND ? AND {ef}""",
                    ('70%', str(start), str(end), *ep)).fetchone()
    return _r(row['n'])


def _has_entries(c, eid, start, end):
    ef, ep = _ef(eid)
    return bool(c.execute(f"SELECT 1 FROM accounting_entries e WHERE e.entry_date BETWEEN ? AND ? AND {ef} AND e.journal_code<>'AN' LIMIT 1",
                          (str(start), str(end), *ep)).fetchone())


# ------------------------------------------------------------------------------- TVA
def _box(payload, code):
    for b in payload.get('boxes') or []:
        if str(b.get('code')) == code:
            return float(b.get('amount') or 0)
    return 0.0


def vat_declared_vs_books(c, eid, since):
    """Chaque CA3 préparée ou déposée depuis `since`, comparée aux écritures actuelles."""
    from profitos.vat_return import ca3_form
    from profitos.teledeclaration import status_label
    rows = c.execute("""SELECT * FROM teledeclarations WHERE entity_id IS ? AND decl_type='TVA_CA3' AND mode<>'simulation'
                        AND period_end>=? AND status IN ('prepared','deposited','acknowledged','accepted','declared','to_verify',
                        'confirmed','filed') ORDER BY period_start, id""", (eid, str(since))).fetchall()
    latest = {}
    for r in rows:  # la dernière version (rectificative comprise) de chaque période
        latest[(r['period_start'], r['period_end'])] = r
    out = []
    for (ps, pe), r in sorted(latest.items()):
        try:
            payload = json.loads(r['payload_json'] or '{}')
        except ValueError:
            continue
        form = ca3_form(c, ps, pe, eid)
        now = {'16': form['l16'], '19': form['l19'], '20': form['l20']}
        gaps = [(code, label, _box(payload, code), int(round(now[code]))) for code, label in CA3_COMPARED
                if abs(_box(payload, code) - int(round(now[code]))) > 1]
        period = f"{_fr(ps)} → {_fr(pe)}"
        filed = r['status'] in FILED
        src = f"CA3 du {period} ({status_label(r['status'], r['mode'])}) et écritures de la période"
        if not gaps:
            out.append(_ck(f'tva_decl:{ps}', f"TVA déclarée du {period} conforme aux écritures", True, '', src))
            continue
        detail = ' ; '.join(f"ligne {code} {label} : déclaré {_eur(d)}, écritures {_eur(n)}" for code, label, d, n in gaps)
        delta = sum(n - d for code, _l, d, n in gaps if code == '16') - sum(n - d for code, _l, d, n in gaps if code in ('19', '20'))
        if filed:
            out.append(_ck(f'tva_decl:{ps}', f"Écritures modifiées après le dépôt de la TVA du {period}", False, detail, src,
                           "Régularisez sur la prochaine CA3 (lignes 21 ou 15 selon le sens) ou déposez une déclaration "
                           "rectificative, puis notez la correction pour l'expert-comptable.", amount=abs(delta)))
        else:
            out.append(_ck(f'tva_decl:{ps}', f"Récapitulatif de TVA du {period} à mettre à jour", None, detail, src,
                           "Préparez à nouveau la déclaration : le récapitulatif figé ne correspond plus aux écritures.",
                           amount=abs(delta)))
    return out


def vat_rates(c, eid, start, end):
    from profitos.accounting import ca3_collected_by_rate
    rates = ca3_collected_by_rate(c, str(start), str(end), eid)
    out = []
    bad = []
    for r in rates:
        if r['rate'] is None or not r['base']:
            continue
        expected = r['base'] * r['rate'] / 100
        if abs(r['vat'] - expected) > max(1.0, 0.01 * abs(expected)):
            bad.append(f"{r['label']} : base {_eur(r['base'])} × {r['rate']:g} % = {_eur(expected)}, TVA comptabilisée {_eur(r['vat'])}")
    src = 'Compte 445710, base et taux mémorisés par ligne'
    if rates:
        out.append(_ck('tva_taux', 'TVA collectée cohérente avec base × taux', None if bad else True, ' ; '.join(bad), src,
                       "Ouvrez le grand livre du 445710 sur l'exercice et corrigez les lignes dont le taux ou la base est faux."
                       if bad else None))
    unsplit = sum(r['vat'] for r in rates if r['rate'] is None)
    if abs(unsplit) >= 1:
        out.append(_ck('tva_non_ventilee', 'TVA collectée sans taux mémorisé', None,
                       f"{_eur(unsplit)} de TVA sans taux ni base (écritures antérieures au suivi par taux ou saisies à la main).",
                       src, "Ventilez ces montants par taux sur la déclaration (lignes 08 à 11) après vérification des pièces.",
                       amount=abs(unsplit)))
    return out


def vat_unliquidated(c, eid, today):
    from profitos.vat_return import unliquidated_before
    first = today.replace(day=1)
    ok = not unliquidated_before(c, first.isoformat(), eid)
    return [_ck('tva_liquidation', 'TVA des mois précédents liquidée', True if ok else False,
                '' if ok else "Des comptes de TVA collectée ou déductible gardent un solde avant ce mois : une période n'a pas été liquidée, "
                              "son crédit éventuel ne sera pas reporté.",
                'Comptes 445710, 445620, 445660', None if ok else
                "Passez l'écriture de liquidation des périodes concernées depuis la déclaration de TVA.")]


def vat_payable_unpaid(c, eid, today):
    """Le 44551 ne doit garder que la TVA de la dernière période liquidée."""
    ef, ep = _ef(eid)
    bal = c.execute(f"""SELECT COALESCE(SUM(l.credit-l.debit),0) n FROM accounting_entry_lines l JOIN accounting_entries e
                        ON e.id=l.entry_id WHERE l.account_code='445510' AND e.entry_date<=? AND {ef}""",
                    (str(today), *ep)).fetchone()['n']
    bal = _r(bal)
    if bal < 1:
        return []
    last = c.execute(f"""SELECT l.credit FROM accounting_entry_lines l JOIN accounting_entries e ON e.id=l.entry_id
                         WHERE l.account_code='445510' AND e.source_type='vat_liquidation' AND {ef}
                         ORDER BY e.entry_date DESC, e.id DESC LIMIT 1""", ep).fetchone()
    last_amount = _r(last['credit']) if last else 0.0
    extra = _r(bal - last_amount)
    ok = extra < 1
    return [_ck('tva_impayee', 'TVA à décaisser réglée pour les périodes antérieures', True if ok else None,
                '' if ok else f"Solde du 445510 : {_eur(bal)}, dont {_eur(extra)} au-delà de la dernière liquidation ({_eur(last_amount)}).",
                'Compte 445510 « TVA à décaisser »', None if ok else
                "Vérifiez sur votre compte fiscal que ces périodes sont payées, puis comptabilisez le paiement (débit 445510, crédit 512).",
                amount=None if ok else extra)]


def vat_turnover(c, eid, start, end):
    from profitos.accounting import ca3_collected_by_rate
    ca = _turnover(c, eid, start, end)
    if ca < 1:
        return []
    base = _r(sum(r['base'] for r in ca3_collected_by_rate(c, str(start), str(end), eid) if r['rate']))
    gap = _r(ca - base)
    src = f"Comptes 70 (chiffre d'affaires) et bases taxées du 445710, du {_fr(start)} au {_fr(end)}"
    tol = max(1000.0, 0.02 * ca)
    if abs(gap) <= tol:
        return [_ck('tva_ca', "Chiffre d'affaires rapproché de la base taxée", True,
                    f"CA {_eur(ca)}, base taxée {_eur(base)}.", src)]
    if gap > 0:
        detail = (f"CA {_eur(ca)}, base taxée {_eur(base)} : {_eur(gap)} de chiffre d'affaires sans TVA collectée. "
                  "Justifications possibles : exportations, livraisons intracommunautaires, opérations exonérées ou "
                  "autoliquidées, TVA sur les encaissements pas encore exigible.")
    else:
        detail = (f"Base taxée {_eur(base)} supérieure au CA {_eur(ca)} de {_eur(-gap)} : acomptes clients taxés, "
                  "chiffre d'affaires comptabilisé sur un autre compte ou TVA collectée en double.")
    return [_ck('tva_ca', "Chiffre d'affaires rapproché de la base taxée", None, detail, src,
                "Documentez l'écart (une ligne par cause) dans le dossier de l'expert-comptable.", amount=abs(gap))]


# ------------------------------------------------------------------------------- IS
def _is_of(c, eid, fy):
    from profitos import corporate_tax as CT
    from profitos.liasse_normal import liasse_normale
    settings = CT.get_fiscal_settings(c, eid, fy['end'].isoformat())
    l = liasse_normale(c, fy['start'], fy['end'], eid, settings=settings)
    eligible = CT.pme_eligible(l['chiffre_affaires'], fy['start'], fy['end'], settings.get('capital_libere'), settings.get('detention_pp'))
    acomptes = settings.get('acomptes_verses')
    if acomptes in (None, ''):
        acomptes = CT.acomptes_from_ledger(c, fy['start'], fy['end'], eid)
    res = CT.compute_is(l['fiscal']['XN'], fy['start'], fy['end'], l['chiffre_affaires'], eligible,
                        credits=settings.get('credits_impot') or 0, acomptes=acomptes)
    return l, res


def corporate_tax(c, eid, closed, current, today, closed_is=None):
    from profitos import corporate_tax as CT
    out = []
    if closed_is is None:
        return out
    src = f"Impôt calculé sur l'exercice clos le {_fr(closed['end'])} et compte 444 (acomptes versés)"
    if closed_is['solde'] < -1:
        out.append(_ck('is_excedent', "Excédent d'acomptes d'IS de l'exercice clos", None,
                       f"Acomptes versés {_eur(closed_is['acomptes_verses'])}, IS dû {_eur(closed_is['is_du'])} : "
                       f"{_eur(-closed_is['solde'])} à récupérer.", src,
                       "Demandez le remboursement avec le relevé de solde 2572, ou imputez-le sur le prochain acompte.",
                       amount=-closed_is['solde']))
    plan = CT.next_instalments(closed_is['is_total'], current['start'], current['end'], closed['start'], closed['end'])
    due = [p for p in plan if p['date'] <= today - timedelta(days=INSTALMENT_GRACE_DAYS)]
    if not due:
        return out
    if len(due) == 1 and due[0]['date'] < CT.liasse_deadline(closed['end'])[0]:
        # Le premier acompte se calcule sur l'avant-dernier exercice : pas de comparaison fiable.
        return out
    expected = _r(sum(p['amount'] for p in due))
    paid = CT.acomptes_from_ledger(c, current['start'], min(today, current['end']), eid)
    ok = paid >= expected - 1
    out.append(_ck('is_acomptes', "Acomptes d'IS de l'exercice en cours versés", True if ok else None,
                   f"{len(due)} acompte(s) exigible(s) au {_fr(due[-1]['date'])} : {_eur(expected)} attendus, {_eur(paid)} comptabilisés.",
                   src, None if ok else
                   "Vérifiez le paiement sur votre compte fiscal et comptabilisez-le (débit 444, crédit 512). Un acompte réduit "
                   "volontairement doit rester suffisant : un retard entraîne une majoration et des intérêts de retard.",
                   amount=None if ok else _r(expected - paid)))
    return out


# --------------------------------------------------------------------------- liasse
def liasse_checks(l, closed):
    src = f"Liasse de l'exercice clos le {_fr(closed['end'])}"
    out = [_ck('liasse_equilibre', 'Bilan équilibré (actif = passif)', True if l['balanced'] else False,
               '' if l['balanced'] else f"Actif {_eur(l['totals_a']['CO_net'])}, passif {_eur(l['totals_p']['EE'])}.", src,
               None if l['balanced'] else "Contrôlez la balance : écritures déséquilibrées ou à-nouveaux incomplets.")]
    for i, msg in enumerate(l.get('checks') or []):
        if msg.startswith('Bilan déséquilibré'):
            continue
        out.append(_ck(f'liasse_{i}', msg.split(' : ')[0].rstrip('.'), None, msg, src,
                       "Rattachez ces comptes à la bonne case ou passez l'écriture manquante avant de produire la liasse."))
    return out


def closing_checks(c, eid, closed, today):
    from profitos import automation as A
    out = []
    for i, p in enumerate(A.precontrols(c, closed['start'], closed['end'], eid, today=today)):
        if p['level'] == 'info' or p['label'].startswith('Comptes de TVA'):  # déjà couvert par la section TVA
            continue
        out.append(_ck(f'cloture_{i}', p['label'], None, p.get('detail') or '', f"Balance de l'exercice clos le {_fr(closed['end'])}"))
    return out


# --------------------------------------------------------------------------- dossier
def _section(key, title, checks, note=''):
    return {'key': key, 'title': title, 'checks': checks, 'level': V.level(checks) if checks else V.READY, 'note': note}


def declarations(c, eid, since):
    from profitos.teledeclaration import status_label, TYPES
    rows = c.execute("""SELECT * FROM teledeclarations WHERE entity_id IS ? AND period_end>=? ORDER BY period_start DESC, id DESC""",
                     (eid, str(since))).fetchall()
    out = []
    for r in rows:
        if r['mode'] == 'simulation':
            transmission = "Simulation : rien n'a été transmis"
        elif r['mode'] == 'manual':
            transmission = ("Déposée par l'entreprise sur impots.gouv.fr (ProfitOS a préparé le récapitulatif)"
                            if r['status'] in FILED else "Préparée par ProfitOS, pas encore déposée")
        else:
            transmission = "Transmise par le partenaire EDI agréé"
        meta = TYPES.get(r['decl_type'], {})
        out.append({'id': r['id'], 'label': meta.get('label', r['decl_type']), 'form': r['form'], 'start': r['period_start'],
                    'end': r['period_end'], 'status': status_label(r['status'], r['mode']), 'transmission': transmission,
                    'amount': r['payment_amount'], 'corrective': bool(r['corrective'])})
    return out


def review(c, eid, today):
    """Dossier complet : sections de contrôles, déclarations, échéancier."""
    from profitos import ledger as L
    from profitos import automation as A
    current = L.fiscal_year_for(c, today, eid)
    closed = L.fiscal_year_for(c, current['start'] - timedelta(days=1), eid)
    closed_active = _has_entries(c, eid, closed['start'], closed['end'])
    sections = []
    vat = (vat_declared_vs_books(c, eid, closed['start']) + vat_unliquidated(c, eid, today) + vat_payable_unpaid(c, eid, today))
    period = (closed['start'], closed['end']) if closed_active else (current['start'], min(today, current['end']))
    vat += vat_rates(c, eid, *period) + vat_turnover(c, eid, *period)
    sections.append(_section('tva', 'TVA', vat, f"Rapprochements sur l'exercice du {_fr(period[0])} au {_fr(period[1])}."))
    closed_is, liasse = None, None
    if closed_active:
        try:
            liasse, closed_is = _is_of(c, eid, closed)
        except Exception:
            liasse, closed_is = None, None
    is_checks = corporate_tax(c, eid, closed, current, today, closed_is)
    sections.append(_section('is', 'Impôt sur les sociétés', is_checks,
                             '' if closed_active else "Aucune écriture sur l'exercice précédent : pas d'IS de référence."))
    if liasse is not None:
        sections.append(_section('liasse', 'Liasse fiscale', liasse_checks(liasse, closed)))
        sections.append(_section('cloture', 'Clôture', closing_checks(c, eid, closed, today)))
    checks = [x for s in sections for x in s['checks']]
    return {
        'today': today, 'current': current, 'closed': closed, 'closed_active': closed_active,
        'sections': sections, 'level': V.level(checks) if checks else V.READY,
        'counts': {'ok': sum(1 for x in checks if x['ok'] is True), 'to_check': sum(1 for x in checks if x['ok'] is None),
                   'blocked': sum(1 for x in checks if x['ok'] is False)},
        'is': closed_is, 'turnover': liasse['chiffre_affaires'] if liasse else None,
        'declarations': declarations(c, eid, closed['start']),
        'calendar': A.upcoming_deadlines(c, eid, today, horizon_days=90),
    }


# ----------------------------------------------------------------- Mission Control
DETECTED = {'tva_decl': 'important', 'tva_impayee': 'important', 'is_acomptes': 'important', 'tva_liquidation': 'info',
            'is_excedent': 'info'}


def detect(c, eid, today):
    """Constats Mission Control : seulement les écarts qui appellent une action précise."""
    data = review(c, eid, today)
    out = []
    for s in data['sections']:
        for ck in s['checks']:
            prefix = ck['code'].split(':')[0]
            if ck['ok'] is True or prefix not in DETECTED:
                continue
            out.append(V.finding(f"{KIND}:{ck['code']}", MODULE, KIND, DETECTED[prefix], ck['label'],
                                 checks=[ck], amount=ck.get('amount'), detail=ck['detail'],
                                 evidence=[V.evidence('Source', ck['source'] or '—', 'Contrôles fiscaux')],
                                 action={'kind': 'link', 'label': 'Ouvrir les contrôles fiscaux', 'endpoint': 'fiscal_controls'},
                                 follow_up_days=7, ref={'code': ck['code']}))
    return out

"""Annexes de la liasse du régime normal :

- 2054 (immobilisations), 2055 (amortissements), 2056 (provisions) avec les codes de
  cases officiels, ligne par ligne ;
- 2059-A (plus et moins-values de cession, à partir des cessions comptabilisées dans
  ProfitOS), 2059-B / 2059-D (néant par défaut), 2059-C (suivi des moins-values à long
  terme) ;
- 2059-F (composition du capital) et 2059-G (filiales et participations), à partir du
  profil fiscal ;
- 2065 (page de garde) : identité, régime, résultat fiscal, renseignements.

Les codes reprennent les formulaires 2054-SD à 2059-G-SD. Une case dont le code n'a pas
pu être établi avec certitude est présentée sans code (montant à reporter à la main).
"""
from datetime import date

from profitos.liasse_normal import _balances, _match, _r

# (libellé, préfixes, codes cadre A [début, réévaluation, acquisitions],
#  codes cadre B [virement, cessions, valeur fin, réévaluation légale], groupe)
IMMO_2054 = [
    ("Frais d'établissement et de développement", ('201', '203'), ('CZ', 'D8', 'D9'), ('IN', 'CØ', 'DØ', 'D7'), 'I'),
    ("Autres postes d'immobilisations incorporelles", ('20', '205', '206', '207', '208', '232', '237'),
     ('KD', 'KE', 'KF'), ('IO', 'LV', 'LW', '1X'), 'II'),
    ('Terrains', ('211', '212'), ('KG', 'KH', 'KI'), ('IP', 'LX', 'LY', 'LZ'), 'III'),
    ('Constructions sur sol propre', ('213', '2131'), ('KJ', 'KK', 'KL'), ('IQ', 'MA', 'MB', 'MC'), 'III'),
    ("Constructions sur sol d'autrui", ('214', '2134'), ('KM', 'KN', 'KO'), ('IR', 'MD', 'ME', 'MF'), 'III'),
    ('Installations générales, agencements et aménagements des constructions', ('2135', '2138'),
     ('KP', 'KQ', 'KR'), ('IS', 'MG', 'MH', 'MI'), 'III'),
    ('Installations techniques, matériel et outillage industriels', ('215',), ('KS', 'KT', 'KU'), ('IT', 'MJ', 'MK', 'ML'), 'III'),
    ('Installations générales, agencements, aménagements divers', ('2181',), ('KV', 'KW', 'KX'), ('IU', 'MM', 'MN', 'MO'), 'III'),
    ('Matériel de transport', ('2182',), ('KY', 'KZ', 'LA'), ('IV', 'MP', 'MQ', 'MR'), 'III'),
    ('Matériel de bureau et informatique, mobilier', ('2183', '2184'), ('LB', 'LC', 'LD'), ('IW', 'MS', 'MT', 'MU'), 'III'),
    ('Emballages récupérables et divers', ('21', '218', '2185', '2186', '2187', '2188', '22'),
     ('LE', 'LF', 'LG'), ('IX', 'MV', 'MW', 'MX'), 'III'),
    ('Immobilisations corporelles en cours', ('23', '231'), ('LH', 'LI', 'LJ'), ('MY', 'MZ', 'NA', 'NB'), 'III'),
    ('Avances et acomptes', ('238',), ('LK', 'LL', 'LM'), ('NC', 'ND', 'NE', 'NF'), 'III'),
    ('Autres participations', ('26', '261', '266', '267', '268'), ('8U', '8V', '8W'), ('ØX', 'ØY', 'ØZ', None), 'IV'),
    ('Autres titres immobilisés', ('271', '272', '273'), ('1P', '1R', '1S'), ('I1', '2B', '2C', '2D'), 'IV'),
    ('Prêts et autres immobilisations financières', ('27', '274', '275', '276'), ('1T', '1U', '1V'), ('I2', '2E', '2F', '2G'), 'IV'),
]
IMMO_TOTALS = {'III': (('LN', 'LO', 'LP'), ('IY', 'NG', 'NH', 'NI'), 'Total III — immobilisations corporelles'),
               'IV': (('LQ', 'LR', 'LS'), ('I3', 'NJ', 'NK', '2H'), 'Total IV — immobilisations financières'),
               'ALL': (('ØG', 'ØH', 'ØJ'), ('I4', 'ØK', 'ØL', 'ØM'), 'Total général')}

# 2055 : (libellé, préfixes des amortissements, codes [début, dotations, diminutions, fin], groupe)
AMORT_2055 = [
    ("Frais d'établissement et de développement", ('2801', '2803'), ('CY', 'EL', 'EM', 'EN'), 'I'),
    ('Fonds commercial', ('2807',), (None, None, None, None), 'II'),
    ('Autres immobilisations incorporelles', ('280', '2805', '2808'), ('PE', 'PF', 'PG', 'PH'), 'II'),
    ('Terrains', ('2811', '2812'), ('PI', 'PJ', 'PK', 'PL'), 'III'),
    ('Constructions sur sol propre', ('2813', '28131'), ('PM', 'PN', 'PO', 'PQ'), 'III'),
    ("Constructions sur sol d'autrui", ('2814', '28134'), ('PR', 'PS', 'PT', 'PU'), 'III'),
    ('Installations générales, agencements et aménagements des constructions', ('28135', '28138'), ('PV', 'PW', 'PX', 'PY'), 'III'),
    ('Installations techniques, matériel et outillage industriels', ('2815',), ('PZ', 'QA', 'QB', 'QC'), 'III'),
    ('Installations générales, agencements, aménagements divers', ('28181',), ('QD', 'QE', 'QF', 'QG'), 'III'),
    ('Matériel de transport', ('28182',), ('QH', 'QI', 'QJ', 'QK'), 'III'),
    ('Matériel de bureau et informatique, mobilier', ('28183', '28184'), ('QL', 'QM', 'QN', 'QO'), 'III'),
    ('Emballages récupérables et divers', ('281', '2818', '28185', '28186', '28187', '28188', '282'), ('QP', 'QR', 'QS', 'QT'), 'III'),
]
AMORT_TOTALS = {'III': (('QU', 'QV', 'QW', 'QX'), 'Total III — immobilisations corporelles'),
                'ALL': (('ØN', 'ØP', 'ØQ', 'ØR'), 'Total général')}

# 2056 : (libellé, préfixes, codes [début, dotations, reprises, fin], groupe)
PROV_2056 = [
    ('Provisions pour hausse des prix', ('143',), ('3V', 'TG', 'TH', 'TI'), 'I'),
    ('Amortissements dérogatoires', ('145',), ('3X', 'TM', 'TN', 'TO'), 'I'),
    ('Autres provisions réglementées', ('14',), ('3Y', 'TP', 'TQ', 'TR'), 'I'),
    ('Provisions pour litiges', ('1511',), ('4A', '4B', '4C', '4D'), 'II'),
    ('Provisions pour garanties données aux clients', ('1512',), ('4E', '4F', '4G', '4H'), 'II'),
    ('Provisions pour pertes sur marchés à terme', ('1513',), ('4J', '4K', '4L', '4M'), 'II'),
    ('Provisions pour amendes et pénalités', ('1514',), ('4N', '4P', '4R', '4S'), 'II'),
    ('Provisions pour pertes de change', ('1515',), ('4T', '4U', '4V', '4W'), 'II'),
    ('Provisions pour pensions et obligations similaires', ('153',), ('4X', '4Y', '4Z', '5A'), 'II'),
    ('Provisions pour impôts', ('155',), ('5B', '5C', '5D', '5E'), 'II'),
    ('Provisions pour gros entretien et grandes révisions', ('157', '1572'), ('EO', 'EP', 'EQ', 'ER'), 'II'),
    ('Autres provisions pour risques et charges', ('15', '1518', '158'), ('5V', '5W', '5X', '5Y'), 'II'),
    ('Dépréciations des immobilisations incorporelles', ('290',), ('6A', '6B', '6C', '6D'), 'III'),
    ('Dépréciations des immobilisations corporelles', ('291', '293'), ('6E', '6F', '6G', '6H'), 'III'),
    ('Dépréciations des titres de participation', ('296', '2961'), ('9U', '9V', '9W', '9X'), 'III'),
    ('Dépréciations des autres immobilisations financières', ('297',), ('Ø6', 'Ø7', 'Ø8', 'Ø9'), 'III'),
    ('Dépréciations des stocks et en-cours', ('39',), ('6N', '6P', '6R', '6S'), 'III'),
    ('Dépréciations des comptes clients', ('491',), ('6T', '6U', '6V', '6W'), 'III'),
    ('Autres dépréciations', ('495', '496', '59'), ('6X', '6Y', '6Z', '7A'), 'III'),
]
PROV_TOTALS = {'I': (('3Z', 'TS', 'TT', 'TU'), 'Total I — provisions réglementées'),
               'II': (('5Z', 'TV', 'TW', 'TX'), 'Total II — provisions pour risques et charges'),
               'III': (('7B', 'TY', 'TZ', 'UA'), 'Total III — dépréciations'),
               'ALL': (('7C', 'UB', 'UC', 'UD'), 'Total général')}


def _assign(bal, spec_prefixes, exclude_starts=()):
    """Affecte chaque compte au libellé dont le préfixe est le plus long."""
    out = {i: {'o': 0.0, 'd': 0.0, 'c': 0.0, 'f': 0.0} for i in range(len(spec_prefixes))}
    for code, b in bal.items():
        if any(code.startswith(x) for x in exclude_starts):
            continue
        best, blen = None, 0
        for i, prefixes in enumerate(spec_prefixes):
            m = _match(code, prefixes)
            if m > blen:
                best, blen = i, m
        if best is not None:
            a = out[best]
            a['o'] += b['opening']; a['d'] += b['debit']; a['c'] += b['credit']; a['f'] += b['closing']
    return out


def tableau_2054(bal):
    agg = _assign(bal, [r[1] for r in IMMO_2054], exclude_starts=('28', '29'))
    rows = []
    for i, (label, _p, codes_a, codes_b, grp) in enumerate(IMMO_2054):
        a = agg[i]
        rows.append({'label': label, 'group': grp, 'codes_a': codes_a, 'codes_b': codes_b,
                     'debut': _r(a['o']), 'reevaluation': 0.0, 'acquisitions': _r(a['d']),
                     'virement': 0.0, 'cessions': _r(a['c']), 'fin': _r(a['f']), 'reeval_legale': 0.0})
    totals = []
    for key, (ca, cb, label) in IMMO_TOTALS.items():
        sel = [r for r in rows if key == 'ALL' or r['group'] == key]
        totals.append({'label': label, 'group': key, 'codes_a': ca, 'codes_b': cb,
                       **{k: _r(sum(r[k] for r in sel)) for k in ('debut', 'reevaluation', 'acquisitions', 'virement',
                                                                  'cessions', 'fin', 'reeval_legale')}})
    return rows, totals


def tableau_2055(bal):
    agg = _assign(bal, [r[1] for r in AMORT_2055])
    rows = []
    for i, (label, _p, codes, grp) in enumerate(AMORT_2055):
        a = agg[i]
        # Comptes d'amortissement créditeurs : dotations au crédit, sorties au débit.
        rows.append({'label': label, 'group': grp, 'codes': codes, 'debut': _r(-a['o']), 'dotations': _r(a['c']),
                     'diminutions': _r(a['d']), 'fin': _r(-a['f'])})
    totals = []
    for key, (codes, label) in AMORT_TOTALS.items():
        sel = [r for r in rows if key == 'ALL' or r['group'] == key]
        totals.append({'label': label, 'group': key, 'codes': codes,
                       **{k: _r(sum(r[k] for r in sel)) for k in ('debut', 'dotations', 'diminutions', 'fin')}})
    return rows, totals


def tableau_2056(bal):
    agg = _assign(bal, [r[1] for r in PROV_2056])
    rows = []
    for i, (label, _p, codes, grp) in enumerate(PROV_2056):
        a = agg[i]
        rows.append({'label': label, 'group': grp, 'codes': codes, 'debut': _r(-a['o']), 'dotations': _r(a['c']),
                     'reprises': _r(a['d']), 'fin': _r(-a['f'])})
    totals = []
    for key, (codes, label) in PROV_TOTALS.items():
        sel = [r for r in rows if key == 'ALL' or r['group'] == key]
        totals.append({'label': label, 'group': key, 'codes': codes,
                       **{k: _r(sum(r[k] for r in sel)) for k in ('debut', 'dotations', 'reprises', 'fin')}})
    s = lambda *pr: _r(sum(b['closing'] for c, b in bal.items() if _match(c, pr)))
    split = [
        {'label': "Dont dotations et reprises d'exploitation", 'codes': ('UE', 'UF'),
         'dotations': s('6815', '6816', '6817'), 'reprises': _r(-s('7815', '7816', '7817'))},
        {'label': 'Dont dotations et reprises financières', 'codes': ('UG', 'UH'),
         'dotations': s('686'), 'reprises': _r(-s('786'))},
        {'label': 'Dont dotations et reprises exceptionnelles', 'codes': ('UJ', 'UK'),
         'dotations': s('687'), 'reprises': _r(-s('787'))},
    ]
    return rows, totals, split


def plus_values_2059a(conn, fy_start, fy_end, entity_id=None):
    """Cessions d'immobilisations de l'exercice. Sociétés à l'IS : plus ou moins-value à
    court terme, sauf titres de participation détenus depuis au moins deux ans (long terme)."""
    ef = 'entity_id=?' if entity_id else 'entity_id IS NULL'
    ep = (entity_id,) if entity_id else ()
    rows = []
    try:
        assets = conn.execute(
            f"""SELECT * FROM fixed_assets WHERE {ef} AND status='disposed' AND disposal_date BETWEEN ? AND ?
                ORDER BY disposal_date""", (*ep, fy_start.isoformat(), fy_end.isoformat())).fetchall()
    except Exception:
        assets = []
    for a in assets:
        amort = conn.execute('SELECT COALESCE(SUM(amount),0) t FROM fixed_asset_depreciation_runs WHERE asset_id=?',
                             (a['id'],)).fetchone()['t']
        gross = _r(a['purchase_amount'])
        amort = min(_r(amort), gross)
        vnc = _r(gross - amort)
        price = _r(a['disposal_price'])
        result = _r(price - vnc)
        held_days = (date.fromisoformat(str(a['disposal_date'])[:10]) - date.fromisoformat(str(a['purchase_date'])[:10])).days
        long_term = str(a['asset_account']).startswith('261') and held_days >= 730
        rows.append({'label': a['label'], 'acquired': a['purchase_date'], 'disposed': a['disposal_date'],
                     'valeur_origine': gross, 'amortissements': amort, 'valeur_residuelle': vnc, 'prix': price,
                     'court_terme': 0.0 if long_term else result, 'long_terme': result if long_term else 0.0})
    return rows, {'court_terme': _r(sum(r['court_terme'] for r in rows)), 'long_terme': _r(sum(r['long_terme'] for r in rows))}


def form_2065(identity, profile, liasse, is_res, start, end):
    f = liasse['fiscal']
    return {
        'denomination': identity.get('name') or '', 'siret': identity.get('siret') or '',
        'adresse': ' '.join(x for x in (identity.get('address') or '', identity.get('postal_code') or '',
                                         identity.get('city') or '') if x).strip(),
        'exercice_debut': start.isoformat(), 'exercice_fin': end.isoformat(),
        'duree_mois': liasse.get('duree_mois'),
        'regime': 'Régime réel normal' if (profile.get('tax_regime') or 'normal') == 'normal' else "Régime simplifié d'imposition",
        'activite': profile.get('activite') or '', 'pme_innovante': bool(profile.get('pme_innovante')),
        'groupe': bool(profile.get('groupe')), 'societe_mere_siren': profile.get('societe_mere_siren') or '',
        'benefice': f['XN'], 'deficit': f['XO'],
        'benefice_taux_normal': is_res['base_taux_normal'], 'benefice_taux_15': is_res['base_taux_reduit'],
        'comptabilite_informatisee': True, 'logiciel': 'ProfitOS',
        'expert_comptable': profile.get('expert_comptable') or '', 'expert_comptable_tel': profile.get('expert_comptable_tel') or '',
        'conseil': profile.get('conseil') or '',
        'signataire': profile.get('signataire') or '', 'signataire_qualite': profile.get('signataire_qualite') or '',
        'mel': profile.get('mel') or '',
        'salaires_bruts': liasse['cr'].get('FY', 0.0),
    }


def capital_2059f(profile):
    rows = [r for r in (profile.get('shareholders') or []) if (r.get('name') or '').strip()]
    pm = [r for r in rows if r.get('kind') == 'PM']
    pp = [r for r in rows if r.get('kind') != 'PM']
    tot = lambda lst: _r(sum(float(r.get('shares') or 0) for r in lst))
    return {'pm': pm, 'pp': pp, 'nb_pm': len(pm), 'parts_pm': tot(pm), 'nb_pp': len(pp), 'parts_pp': tot(pp),
            'pct_total': _r(sum(float(r.get('pct') or 0) for r in rows))}


def filiales_2059g(profile):
    rows = [r for r in (profile.get('subsidiaries') or []) if (r.get('name') or '').strip()]
    return {'rows': rows, 'count': len(rows)}


def annexes(conn, fy_start, fy_end, entity_id=None, profile=None, settings=None):
    profile = profile or {}
    settings = settings or {}
    bal = _balances(conn, fy_start, fy_end, entity_id)
    immo, immo_tot = tableau_2054(bal)
    amort, amort_tot = tableau_2055(bal)
    prov, prov_tot, prov_split = tableau_2056(bal)
    pv, pv_tot = plus_values_2059a(conn, fy_start, fy_end, entity_id)
    mvlt = _r(settings.get('mvlt_anterieures') or 0)
    checks = []
    if pv_tot['long_terme']:
        checks.append("Plus ou moins-value à long terme sur titres de participation : vérifiez la quote-part de frais et "
                      "charges (12 %) à réintégrer au 2058-A.")
    cap = capital_2059f(profile)
    if cap['pp'] or cap['pm']:
        if abs(cap['pct_total'] - 100) > 0.01 and cap['pct_total'] > 0:
            checks.append(f"Composition du capital (2059-F) : les pourcentages déclarés totalisent {cap['pct_total']:g} %. "
                          "Seuls les associés détenant au moins 10 % sont obligatoires ; vérifiez la saisie.")
    return {
        'immo_2054': immo, 'immo_2054_tot': immo_tot, 'amort_2055': amort, 'amort_2055_tot': amort_tot,
        'prov_2056': prov, 'prov_2056_tot': prov_tot, 'prov_2056_split': prov_split,
        'pv_2059a': pv, 'pv_2059a_tot': pv_tot,
        'mvlt_2059c': {'anterieures': mvlt, 'imputees': _r(min(mvlt, max(0.0, pv_tot['long_terme']))),
                       'solde': _r(mvlt - min(mvlt, max(0.0, pv_tot['long_terme'])) + max(0.0, -pv_tot['long_terme']))},
        'capital_2059f': cap, 'filiales_2059g': filiales_2059g(profile), 'checks': checks,
    }


def annexes_rows(ax, f2065=None):
    """Lignes d'export (formulaire, case, libellé, montant arrondi à l'euro)."""
    e = lambda x: int(round(float(x or 0)))
    rows = []
    for r in ax['immo_2054'] + ax['immo_2054_tot']:
        for code, key, col in zip(r['codes_a'], ('debut', 'reevaluation', 'acquisitions'),
                                  ("valeur brute début", 'réévaluation', 'acquisitions')):
            if code:
                rows.append({'Formulaire': '2054', 'Case': code, 'Libellé': f"{r['label']} — {col}", 'Montant': e(r[key])})
        for code, key, col in zip(r['codes_b'], ('virement', 'cessions', 'fin', 'reeval_legale'),
                                  ('virement de poste à poste', 'cessions', 'valeur brute fin', 'réévaluation légale')):
            if code:
                rows.append({'Formulaire': '2054', 'Case': code, 'Libellé': f"{r['label']} — {col}", 'Montant': e(r[key])})
    for r in ax['amort_2055'] + ax['amort_2055_tot']:
        for code, key, col in zip(r['codes'], ('debut', 'dotations', 'diminutions', 'fin'),
                                  ('début', 'dotations', 'diminutions', 'fin')):
            if code:
                rows.append({'Formulaire': '2055', 'Case': code, 'Libellé': f"{r['label']} — {col}", 'Montant': e(r[key])})
    for r in ax['prov_2056'] + ax['prov_2056_tot']:
        for code, key, col in zip(r['codes'], ('debut', 'dotations', 'reprises', 'fin'),
                                  ('début', 'dotations', 'reprises', 'fin')):
            rows.append({'Formulaire': '2056', 'Case': code, 'Libellé': f"{r['label']} — {col}", 'Montant': e(r[key])})
    for r in ax['prov_2056_split']:
        rows.append({'Formulaire': '2056', 'Case': r['codes'][0], 'Libellé': f"{r['label']} — dotations", 'Montant': e(r['dotations'])})
        rows.append({'Formulaire': '2056', 'Case': r['codes'][1], 'Libellé': f"{r['label']} — reprises", 'Montant': e(r['reprises'])})
    for i, r in enumerate(ax['pv_2059a'], 1):
        rows.append({'Formulaire': '2059-A', 'Case': f'L{i}', 'Libellé': f"{r['label']} (cédé le {r['disposed']}) — résultat",
                     'Montant': e(r['court_terme'] + r['long_terme'])})
    rows.append({'Formulaire': '2059-A', 'Case': 'A', 'Libellé': 'Cadre A — plus ou moins-value nette à court terme',
                 'Montant': e(ax['pv_2059a_tot']['court_terme'])})
    rows.append({'Formulaire': '2059-A', 'Case': 'B', 'Libellé': 'Cadre B — plus ou moins-value nette à long terme',
                 'Montant': e(ax['pv_2059a_tot']['long_terme'])})
    cap = ax['capital_2059f']
    rows.append({'Formulaire': '2059-F', 'Case': '', 'Libellé': 'Nombre total d\'associés personnes morales', 'Montant': cap['nb_pm']})
    rows.append({'Formulaire': '2059-F', 'Case': '', 'Libellé': 'Nombre total d\'associés personnes physiques', 'Montant': cap['nb_pp']})
    rows.append({'Formulaire': '2059-G', 'Case': '', 'Libellé': 'Nombre total de filiales détenues', 'Montant': ax['filiales_2059g']['count']})
    if f2065:
        rows.append({'Formulaire': '2065', 'Case': '', 'Libellé': 'Résultat fiscal — bénéfice', 'Montant': e(f2065['benefice'])})
        rows.append({'Formulaire': '2065', 'Case': '', 'Libellé': 'Résultat fiscal — déficit', 'Montant': e(f2065['deficit'])})
        rows.append({'Formulaire': '2065', 'Case': '', 'Libellé': 'Bénéfice imposable au taux normal', 'Montant': e(f2065['benefice_taux_normal'])})
        rows.append({'Formulaire': '2065', 'Case': '', 'Libellé': 'Bénéfice imposable à 15 %', 'Montant': e(f2065['benefice_taux_15'])})
    return rows

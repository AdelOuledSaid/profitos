"""Annexes de la liasse du régime simplifié (2033-C à 2033-G) et détermination du
résultat fiscal du 2033-B :

- 2033-B, cadre B : réintégrations et déductions extra-comptables, imputation des
  déficits, résultat fiscal (mêmes paramètres que le 2058-A du régime normal) ;
- 2033-C : immobilisations (valeurs brutes), amortissements, plus et moins-values de
  cession ;
- 2033-D : provisions et dépréciations, suivi des déficits ;
- 2033-E : valeur ajoutée (CVAE) ;
- 2033-F : composition du capital, 2033-G : filiales et participations (profil fiscal).

Les montants sont tirés de la balance, comme pour le régime normal. Seuls les codes de
cases établis avec certitude sont indiqués ; les autres lignes sont présentées par
rubrique, dans l'ordre du formulaire.
"""
from profitos.liasse_normal import _balances, _match, _r, liasse_normale, VA_LABELS
from profitos.liasse_annexes import plus_values_2059a, capital_2059f, filiales_2059g

# 2033-C cadre I : (libellé, préfixes du brut)
IMMO_2033 = [
    ('Fonds commercial', ('207',)),
    ('Autres immobilisations incorporelles', ('20', '232', '237')),
    ('Terrains', ('211', '212')),
    ('Constructions', ('213', '214')),
    ('Installations techniques, matériel et outillage industriels', ('215',)),
    ('Installations générales, agencements, aménagements divers', ('2181', '2135', '2138')),
    ('Matériel de transport', ('2182',)),
    ('Autres immobilisations corporelles', ('21', '218', '22', '23')),
    ('Immobilisations financières', ('26', '27')),
]
# 2033-C cadre II : (libellé, préfixes des amortissements)
AMORT_2033 = [
    ('Immobilisations incorporelles', ('280',)),
    ('Terrains', ('2811', '2812')),
    ('Constructions', ('2813', '2814')),
    ('Installations techniques, matériel et outillage industriels', ('2815',)),
    ('Installations générales, agencements, aménagements divers', ('28181', '28135', '28138')),
    ('Matériel de transport', ('28182',)),
    ('Autres immobilisations corporelles', ('281', '2818', '282')),
]
# 2033-D cadre I : (libellé, préfixes)
PROV_2033 = [
    ('Provisions réglementées — amortissements dérogatoires', ('145',)),
    ('Provisions réglementées — autres', ('14',)),
    ('Provisions pour risques et charges', ('15',)),
    ('Dépréciations des immobilisations', ('29',)),
    ('Dépréciations des stocks et en-cours', ('39',)),
    ('Dépréciations des comptes clients', ('491',)),
    ('Autres dépréciations', ('49', '59')),
]


def _group(bal, spec, exclude=()):
    """Affecte chaque compte au préfixe le plus long de la spécification."""
    out = [{'o': 0.0, 'd': 0.0, 'c': 0.0, 'f': 0.0} for _ in spec]
    for code, b in bal.items():
        if any(code.startswith(x) for x in exclude):
            continue
        best, blen = None, 0
        for i, (_label, prefixes) in enumerate(spec):
            m = _match(code, prefixes)
            if m > blen:
                best, blen = i, m
        if best is not None:
            a = out[best]
            a['o'] += b['opening']; a['d'] += b['debit']; a['c'] += b['credit']; a['f'] += b['closing']
    return out


def _total(rows, keys):
    return {k: _r(sum(r[k] for r in rows)) for k in keys}


def annexes_2033(conn, fy_start, fy_end, entity_id=None, profile=None, settings=None, liasse=None):
    profile = profile or {}
    settings = settings or {}
    bal = _balances(conn, fy_start, fy_end, entity_id)
    l = liasse or liasse_normale(conn, fy_start, fy_end, entity_id, settings=settings)

    immo = []
    for (label, _p), a in zip(IMMO_2033, _group(bal, IMMO_2033, exclude=('28', '29'))):
        immo.append({'label': label, 'debut': _r(a['o']), 'augmentations': _r(a['d']),
                     'diminutions': _r(a['c']), 'fin': _r(a['f'])})
    amort = []
    for (label, _p), a in zip(AMORT_2033, _group(bal, AMORT_2033)):
        amort.append({'label': label, 'debut': _r(-a['o']), 'dotations': _r(a['c']),
                      'diminutions': _r(a['d']), 'fin': _r(-a['f'])})
    prov = []
    for (label, _p), a in zip(PROV_2033, _group(bal, PROV_2033)):
        prov.append({'label': label, 'debut': _r(-a['o']), 'dotations': _r(a['c']),
                     'reprises': _r(a['d']), 'fin': _r(-a['f'])})
    pv, pv_tot = plus_values_2059a(conn, fy_start, fy_end, entity_id)

    f = l['fiscal']
    d = f['2058B']
    deficits = [
        {'code': '982', 'label': "Déficits restant à reporter au titre de l'exercice précédent", 'amount': d['K4']},
        {'code': '983', 'label': "Déficits imputés sur le résultat de l'exercice", 'amount': d['K5']},
        {'code': '984', 'label': 'Déficits reportables', 'amount': d['K6']},
        {'code': '860', 'label': "Déficit de l'exercice", 'amount': d['YJ']},
        {'code': '870', 'label': 'Total des déficits restant à reporter', 'amount': d['YK']},
    ]
    resultat_fiscal = {
        'resultat_comptable': l['cr']['HN'],
        'reintegrations': [x for x in f['reintegrations'] if x['code'] != 'WA'],
        'deductions': [x for x in f['deductions'] if x['code'] != 'WS'],
        'avant_deficits': _r(f['XI'] - f['XJ']),
        'deficits_imputes': f['XL'],
        'benefice': f['XN'], 'deficit': f['XO'],
    }
    va_boxes = ['OA', 'OK', 'OT', 'OX', 'OH', 'OE', 'OF', 'OD', 'XT', 'OM', 'ON', 'OQ', 'OR', 'OZ', 'OW', 'OJ', 'OG']
    va = [{'key': k, 'label': VA_LABELS[k], 'amount': l['va'][k], 'total': k in ('OX', 'OM', 'OJ', 'OG')} for k in va_boxes]

    checks = []
    cap = capital_2059f(profile)
    if (cap['pm'] or cap['pp']) and cap['pct_total'] and abs(cap['pct_total'] - 100) > 0.01:
        checks.append(f"Composition du capital (2033-F) : les pourcentages déclarés totalisent {cap['pct_total']:g} %. "
                      "Seuls les associés détenant au moins 10 % sont obligatoires ; vérifiez la saisie.")
    if pv_tot['long_terme']:
        checks.append("Plus ou moins-value à long terme sur titres de participation : vérifiez la quote-part de frais et "
                      "charges (12 %) à réintégrer au 2033-B.")
    return {
        'immo': immo, 'immo_tot': _total(immo, ('debut', 'augmentations', 'diminutions', 'fin')),
        'amort': amort, 'amort_tot': _total(amort, ('debut', 'dotations', 'diminutions', 'fin')),
        'pv': pv, 'pv_tot': pv_tot,
        'prov': prov, 'prov_tot': _total(prov, ('debut', 'dotations', 'reprises', 'fin')),
        'deficits': deficits, 'resultat_fiscal': resultat_fiscal,
        'va': va, 'chiffre_affaires': l['va']['OX'], 'valeur_ajoutee': l['va']['OG'],
        'capital': cap, 'filiales': filiales_2059g(profile), 'checks': checks,
    }


def annexes_2033_rows(ax, f2065=None):
    """Lignes d'export (tableau, case, libellé, montant arrondi à l'euro)."""
    e = lambda x: int(round(float(x or 0)))
    rows = []
    rf = ax['resultat_fiscal']
    rows.append({'Tableau': '2033-B', 'Case': '310', 'Libellé': 'Résultat comptable', 'Montant': e(rf['resultat_comptable'])})
    for x in rf['reintegrations']:
        rows.append({'Tableau': '2033-B', 'Case': '', 'Libellé': f"Réintégration — {x['label']}", 'Montant': e(x['amount'])})
    for x in rf['deductions']:
        rows.append({'Tableau': '2033-B', 'Case': '', 'Libellé': f"Déduction — {x['label']}", 'Montant': e(x['amount'])})
    rows.append({'Tableau': '2033-B', 'Case': '', 'Libellé': 'Déficits antérieurs imputés', 'Montant': e(rf['deficits_imputes'])})
    rows.append({'Tableau': '2033-B', 'Case': '370', 'Libellé': 'Résultat fiscal — bénéfice', 'Montant': e(rf['benefice'])})
    rows.append({'Tableau': '2033-B', 'Case': '372', 'Libellé': 'Résultat fiscal — déficit', 'Montant': e(rf['deficit'])})
    for r in ax['immo'] + [dict(ax['immo_tot'], label='Total')]:
        for key, col in (('debut', 'valeur brute début'), ('augmentations', 'augmentations'),
                         ('diminutions', 'diminutions'), ('fin', 'valeur brute fin')):
            rows.append({'Tableau': '2033-C', 'Case': '', 'Libellé': f"Immobilisations — {r['label']} — {col}", 'Montant': e(r[key])})
    for r in ax['amort'] + [dict(ax['amort_tot'], label='Total')]:
        for key, col in (('debut', 'début'), ('dotations', 'dotations'), ('diminutions', 'diminutions'), ('fin', 'fin')):
            rows.append({'Tableau': '2033-C', 'Case': '', 'Libellé': f"Amortissements — {r['label']} — {col}", 'Montant': e(r[key])})
    for r in ax['pv']:
        rows.append({'Tableau': '2033-C', 'Case': '', 'Libellé': f"Cession — {r['label']} (cédé le {r['disposed']}) — résultat",
                     'Montant': e(r['court_terme'] + r['long_terme'])})
    rows.append({'Tableau': '2033-C', 'Case': '', 'Libellé': 'Plus ou moins-values nettes à court terme', 'Montant': e(ax['pv_tot']['court_terme'])})
    rows.append({'Tableau': '2033-C', 'Case': '', 'Libellé': 'Plus ou moins-values nettes à long terme', 'Montant': e(ax['pv_tot']['long_terme'])})
    for r in ax['prov'] + [dict(ax['prov_tot'], label='Total')]:
        for key, col in (('debut', 'début'), ('dotations', 'dotations'), ('reprises', 'reprises'), ('fin', 'fin')):
            rows.append({'Tableau': '2033-D', 'Case': '', 'Libellé': f"{r['label']} — {col}", 'Montant': e(r[key])})
    for x in ax['deficits']:
        rows.append({'Tableau': '2033-D', 'Case': x['code'], 'Libellé': x['label'], 'Montant': e(x['amount'])})
    for x in ax['va']:
        rows.append({'Tableau': '2033-E', 'Case': '', 'Libellé': x['label'], 'Montant': e(x['amount'])})
    cap = ax['capital']
    rows.append({'Tableau': '2033-F', 'Case': '', 'Libellé': "Nombre d'associés personnes morales", 'Montant': cap['nb_pm']})
    rows.append({'Tableau': '2033-F', 'Case': '', 'Libellé': "Nombre d'associés personnes physiques", 'Montant': cap['nb_pp']})
    rows.append({'Tableau': '2033-G', 'Case': '', 'Libellé': 'Nombre de filiales et participations', 'Montant': ax['filiales']['count']})
    if f2065:
        rows.append({'Tableau': '2065', 'Case': '', 'Libellé': 'Résultat fiscal — bénéfice', 'Montant': e(f2065['benefice'])})
        rows.append({'Tableau': '2065', 'Case': '', 'Libellé': 'Résultat fiscal — déficit', 'Montant': e(f2065['deficit'])})
        rows.append({'Tableau': '2065', 'Case': '', 'Libellé': 'Bénéfice imposable au taux normal', 'Montant': e(f2065['benefice_taux_normal'])})
        rows.append({'Tableau': '2065', 'Case': '', 'Libellé': 'Bénéfice imposable à 15 %', 'Montant': e(f2065['benefice_taux_15'])})
    return rows

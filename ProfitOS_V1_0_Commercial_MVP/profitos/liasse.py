"""Liasse fiscale simplifiée (régime réel simplifié) : tableaux 2033-A (bilan) et
2033-B (compte de résultat) pré-remplis à partir de la balance de l'exercice.

Aide au remplissage : la correspondance comptes → cases suit le plan comptable
général et la notice 2033, mais la liasse doit être revue (et transmise en EDI-TDFC)
par l'expert-comptable ou via un partenaire EDI. Les cases non alimentées
automatiquement (crédit-bail, plus-values, déficits…) restent à compléter.
"""
from profitos.ledger import trial_balance

# ---------------------------------------------------------------------------
# 2033-A — Bilan simplifié. Actif : (case brut, case amort./dépréciations, libellé,
# préfixes du brut, préfixes des amortissements/dépréciations).
# ---------------------------------------------------------------------------
ACTIF = [
    ('010', '012', 'Fonds commercial', ('207',), ('2807', '2907')),
    ('014', '016', 'Autres immobilisations incorporelles', ('20',), ('280', '290')),
    ('028', '030', 'Immobilisations corporelles', ('21', '22', '23'), ('281', '282', '291', '293')),
    ('040', '042', 'Immobilisations financières', ('26', '27'), ('296', '297')),
    ('050', '052', 'Stocks de matières premières, en-cours et produits', ('31', '32', '33', '34', '35', '36'),
     ('391', '392', '393', '394', '395', '396')),
    ('060', '062', 'Stocks de marchandises', ('37',), ('397',)),
    ('064', None, 'Avances et acomptes versés sur commandes', ('4091',), ()),
    ('068', '070', 'Créances clients et comptes rattachés', ('411', '413', '416', '417', '418'), ('491',)),
    ('080', '082', 'Valeurs mobilières de placement', ('50',), ('59',)),
    ('084', None, 'Disponibilités', ('51', '53', '54', '58'), ()),
    ('092', None, "Charges constatées d'avance", ('486',), ()),
]
# Autres créances : soldes débiteurs des autres comptes de tiers (classe 4).
OTHER_RECEIVABLES = ('072', '074', 'Autres créances')

# Passif : (case, libellé, préfixes). Le résultat est calculé (case 136).
PASSIF = [
    ('120', 'Capital social ou individuel', ('101', '108')),
    ('124', 'Écarts de réévaluation', ('105',)),
    ('126', 'Réserve légale', ('1061',)),
    ('130', 'Réserves réglementées', ('1062', '1064')),
    ('132', 'Autres réserves', ('104', '1063', '1068')),
    ('134', 'Report à nouveau', ('11',)),
    ('140', "Provisions réglementées et subventions d'investissement", ('13', '14')),
    ('154', 'Provisions pour risques et charges', ('15',)),
    ('156', 'Emprunts et dettes assimilées', ('16', '17', '519')),
    ('164', 'Avances et acomptes reçus sur commandes en cours', ('4191',)),
    ('166', 'Fournisseurs et comptes rattachés', ('401', '403', '408')),
    ('174', "Produits constatés d'avance", ('487',)),
]
OTHER_DEBTS = ('172', 'Autres dettes')

# ---------------------------------------------------------------------------
# 2033-B — Compte de résultat simplifié : (case, libellé, préfixes, sens) ;
# sens 'P' = produit (crédit − débit), 'C' = charge (débit − crédit).
# ---------------------------------------------------------------------------
PRODUITS_EXPLOITATION = [
    ('210', 'Ventes de marchandises', ('707',), 'P'),
    ('214', 'Production vendue — biens', ('701', '702', '703'), 'P'),
    ('218', 'Production vendue — services', ('704', '705', '706', '708', '709'), 'P'),
    ('222', 'Production stockée', ('713',), 'P'),
    ('224', 'Production immobilisée', ('72',), 'P'),
    ('226', "Subventions d'exploitation", ('74',), 'P'),
    ('230', 'Autres produits (dont reprises et transferts de charges)', ('75', '781', '791'), 'P'),
]
CHARGES_EXPLOITATION = [
    ('234', 'Achats de marchandises', ('607', '6087', '6097'), 'C'),
    ('236', 'Variation de stock (marchandises)', ('6037',), 'C'),
    ('238', 'Achats de matières premières et autres approvisionnements', ('601', '602', '6081', '6082', '6091', '6092'), 'C'),
    ('240', 'Variation de stock (matières premières et approvisionnements)', ('6031', '6032'), 'C'),
    ('242', 'Autres charges externes', ('604', '605', '606', '6084', '6085', '6086', '6094', '6095', '6096', '61', '62'), 'C'),
    ('244', 'Impôts, taxes et versements assimilés', ('63',), 'C'),
    ('250', 'Rémunérations du personnel', ('641', '644', '648'), 'C'),
    ('252', 'Charges sociales', ('645', '646', '647'), 'C'),
    ('254', 'Dotations aux amortissements', ('6811', '6812'), 'C'),
    ('256', 'Dotations aux provisions et dépréciations', ('6815', '6816', '6817'), 'C'),
    ('262', 'Autres charges', ('65',), 'C'),
]
FINANCIER_EXCEPTIONNEL = [
    ('280', 'Produits financiers', ('76', '786', '796'), 'P'),
    ('290', 'Produits exceptionnels', ('77', '787', '797'), 'P'),
    ('294', 'Charges financières', ('66', '686'), 'C'),
    ('300', 'Charges exceptionnelles', ('67', '687'), 'C'),
    ('306', 'Impôts sur les bénéfices (et participation des salariés)', ('69',), 'C'),
]


def _match(code, prefixes):
    """Préfixe le plus long qui correspond (0 si aucun)."""
    return max((len(p) for p in prefixes if code.startswith(p)), default=0)


def liasse_2033(conn, fy_start, fy_end, entity_id=None):
    tb = trial_balance(conn, fy_start, fy_end, entity_id, 'generale')
    balances = {l['key']: round(l['closing_debit'] - l['closing_credit'], 2) for l in tb['lines']}
    used = set()

    # --- Compte de résultat
    def cr_section(spec):
        out = []
        for box, label, prefixes, sens in spec:
            amount = 0.0
            for code, bal in balances.items():
                if code[0] in '67' and _match(code, prefixes) and _best(code, spec) == box:
                    amount += -bal if sens == 'P' else bal
                    used.add(code)
            out.append({'box': box, 'label': label, 'amount': round(amount, 2)})
        return out

    def _best(code, spec):
        best, best_len = None, 0
        for box, _l, prefixes, _s in spec:
            m = _match(code, prefixes)
            if m > best_len:
                best, best_len = box, m
        return best

    prod = cr_section(PRODUITS_EXPLOITATION)
    charges = cr_section(CHARGES_EXPLOITATION)
    finexc = cr_section(FINANCIER_EXCEPTIONNEL)
    total_prod = round(sum(x['amount'] for x in prod), 2)
    total_charges = round(sum(x['amount'] for x in charges), 2)
    resultat_exploitation = round(total_prod - total_charges, 2)
    fe = {x['box']: x['amount'] for x in finexc}
    # Comptes de gestion non classés (ex. compte personnalisé) : rattachés aux autres
    # produits / autres charges pour que le résultat reste exact.
    unclassified = [(c, b) for c, b in balances.items() if c[0] in '67' and c not in used and abs(b) >= 0.005]
    for code, bal in unclassified:
        target = prod if code[0] == '7' else charges
        line = next(x for x in target if x['box'] == ('230' if code[0] == '7' else '262'))
        line['amount'] = round(line['amount'] + (-bal if code[0] == '7' else bal), 2)
    if unclassified:
        total_prod = round(sum(x['amount'] for x in prod), 2)
        total_charges = round(sum(x['amount'] for x in charges), 2)
        resultat_exploitation = round(total_prod - total_charges, 2)
    resultat = round(resultat_exploitation + fe['280'] + fe['290'] - fe['294'] - fe['300'] - fe['306'], 2)

    # --- Bilan : chaque compte de bilan va dans UN seul poste (le préfixe le plus long)
    actif = [{'box_brut': b, 'box_amort': a, 'label': lab, 'brut': 0.0, 'amort': 0.0, 'net': 0.0}
             for b, a, lab, _bp, _ap in ACTIF]
    passif = [{'box': b, 'label': lab, 'amount': 0.0} for b, lab, _p in PASSIF]
    other_rec = other_debt = dep_other = prior_result = 0.0
    unassigned = []
    for code, bal in balances.items():
        if code[0] not in '12345' or abs(bal) < 0.005:
            continue
        if code.startswith('12'):  # résultat d'un exercice clôturé, non encore affecté
            prior_result += -bal
            continue
        if code.startswith(('495', '496')):
            dep_other += -bal
            continue
        best = (0, None, None)  # (longueur, poste, nature)
        for i, (_b, _a, _lab, brut_p, amort_p) in enumerate(ACTIF):
            for kind, prefixes in (('brut', brut_p), ('amort', amort_p)):
                m = _match(code, prefixes)
                if m > best[0]:
                    best = (m, ('A', i), kind)
        for i, (_b, _lab, prefixes) in enumerate(PASSIF):
            m = _match(code, prefixes)
            if m > best[0]:
                best = (m, ('P', i), 'passif')
        target, kind = best[1], best[2]
        if target and target[0] == 'A' and kind == 'brut' and ACTIF[target[1]][0] == '084' and bal < 0:
            target, kind = ('P', _passif_index('156')), 'passif'      # banque créditrice
        elif target and target[0] == 'P' and PASSIF[target[1]][0] == '156' and code[0] == '5' and bal > 0:
            target, kind = ('A', _actif_index('084')), 'brut'
        elif target and target[0] == 'P' and code[0] == '4' and bal > 0:
            target = None                                               # tiers débiteur : autre créance
        elif target and target[0] == 'A' and kind == 'brut' and code[0] == '4' and bal < 0:
            target = None                                               # client créditeur : autre dette
        if target is None:
            if code[0] == '4':
                if bal > 0:
                    other_rec += bal
                else:
                    other_debt += -bal
            else:
                unassigned.append(code)
            continue
        if target[0] == 'A':
            row = actif[target[1]]
            if kind == 'amort':
                row['amort'] += -bal
            else:
                row['brut'] += bal
        else:
            passif[target[1]]['amount'] += -bal
    for row in actif:
        row['brut'], row['amort'] = round(row['brut'], 2), round(row['amort'], 2)
        row['net'] = round(row['brut'] - row['amort'], 2)
    actif.insert(_actif_index('080'), {'box_brut': OTHER_RECEIVABLES[0], 'box_amort': OTHER_RECEIVABLES[1],
                                      'label': OTHER_RECEIVABLES[2], 'brut': round(other_rec, 2),
                                      'amort': round(dep_other, 2), 'net': round(other_rec - dep_other, 2)})
    passif.insert(_passif_index('140'), {'box': '136', 'label': "Résultat de l'exercice",
                                         'amount': round(resultat + prior_result, 2)})
    passif.insert(next(i for i, p in enumerate(passif) if p['box'] == '174'),
                  {'box': OTHER_DEBTS[0], 'label': OTHER_DEBTS[1], 'amount': round(other_debt, 2)})
    for p in passif:
        p['amount'] = round(p['amount'], 2)
    total_actif = round(sum(a['net'] for a in actif), 2)
    capitaux = round(sum(p['amount'] for p in passif if p['box'] in ('120', '124', '126', '130', '132', '134', '136', '140')), 2)
    total_passif = round(sum(p['amount'] for p in passif), 2)
    return {
        'actif': actif, 'passif': passif, 'total_actif': total_actif, 'total_passif': total_passif,
        'capitaux_propres': capitaux, 'balanced': abs(total_actif - total_passif) < 0.5,
        'produits': prod, 'charges': charges, 'total_produits': total_prod, 'total_charges': total_charges,
        'resultat_exploitation': resultat_exploitation, 'financier_exceptionnel': finexc, 'resultat': resultat,
        'unclassified': [c for c, _ in unclassified], 'unassigned': unassigned,
    }


def _actif_index(box):
    return next(i for i, row in enumerate(ACTIF) if row[0] == box)


def _passif_index(box):
    return next(i for i, row in enumerate(PASSIF) if row[0] == box)


def liasse_rows(l):
    rows = []
    for a in l['actif']:
        rows.append({'Tableau': '2033-A Actif', 'Case': a['box_brut'], 'Libellé': a['label'], 'Brut': a['brut'],
                     'Case amort.': a['box_amort'] or '', 'Amortissements / dépréciations': a['amort'], 'Net': a['net']})
    for p in l['passif']:
        rows.append({'Tableau': '2033-A Passif', 'Case': p['box'], 'Libellé': p['label'], 'Net': p['amount']})
    for x in l['produits'] + l['charges'] + l['financier_exceptionnel']:
        rows.append({'Tableau': '2033-B', 'Case': x['box'], 'Libellé': x['label'], 'Net': x['amount']})
    rows.append({'Tableau': '2033-B', 'Case': '310', 'Libellé': 'Bénéfice ou perte', 'Net': l['resultat']})
    return rows

"""Liasse fiscale du régime réel normal (BIC/IS) : tableaux 2050 à 2059 pré-remplis
à partir de la comptabilité.

- 2050 / 2051 : bilan actif et passif (brut, amortissements, net ; net N-1) ;
- 2052 / 2053 : compte de résultat ;
- 2054 / 2055 / 2056 : immobilisations, amortissements, provisions (mouvements de
  l'exercice, à-nouveaux compris dans le solde d'ouverture) ;
- 2057 : échéances des créances et des dettes (échéancier des emprunts pour le
  découpage à un an / un à cinq ans / plus de cinq ans) ;
- 2058-A / 2058-B / 2058-C : résultat fiscal, suivi des déficits, affectation et
  renseignements divers ;
- 2059-E : valeur ajoutée (base de la CVAE).

Les codes de cases suivent les formulaires 2050-SD à 2059-SD. Les montants sont
arrondis à l'euro comme sur le formulaire. C'est une aide au remplissage : la liasse
doit être relue (réintégrations et déductions extra-comptables notamment) avant
d'être déposée par EDI-TDFC.
"""
from datetime import date, timedelta

from profitos.ledger import trial_balance

# ---------------------------------------------------------------------------
# Correspondance comptes -> cases. Un compte va dans le poste dont le préfixe est le
# plus long ; les préfixes courts servent de rattrapage.
# ---------------------------------------------------------------------------
ACTIF_2050 = [
    # (case brut, case amort., libellé, préfixes brut, préfixes amort./dépréciations, rubrique)
    ('AA', None, 'Capital souscrit non appelé', ('109',), (), 'capital'),
    ('AB', 'AC', "Frais d'établissement", ('201',), ('2801',), 'incorporel'),
    ('CX', 'CQ', 'Frais de développement', ('203',), ('2803', '2903'), 'incorporel'),
    ('AF', 'AG', 'Concessions, brevets et droits similaires', ('205',), ('2805', '2905'), 'incorporel'),
    ('AH', 'AI', 'Fonds commercial', ('206', '207'), ('2806', '2807', '2906', '2907'), 'incorporel'),
    ('AJ', 'AK', 'Autres immobilisations incorporelles', ('20', '208'), ('280', '2808', '290', '2908'), 'incorporel'),
    ('AL', 'AM', 'Avances et acomptes sur immobilisations incorporelles', ('237',), ('2932',), 'incorporel'),
    ('AN', 'AO', 'Terrains', ('211', '212'), ('2811', '2812', '2911'), 'corporel'),
    ('AP', 'AQ', 'Constructions', ('213', '214'), ('2813', '2814'), 'corporel'),
    ('AR', 'AS', 'Installations techniques, matériel et outillage industriels', ('215',), ('2815',), 'corporel'),
    ('AT', 'AU', 'Autres immobilisations corporelles', ('21', '218', '22'), ('281', '2818', '282', '291'), 'corporel'),
    ('AV', 'AW', 'Immobilisations en cours', ('23', '231', '232'), ('293', '2931'), 'corporel'),
    ('AX', 'AY', 'Avances et acomptes', ('238',), (), 'corporel'),
    ('CU', 'CV', 'Autres participations', ('26', '261', '266'), ('296', '2961', '2966'), 'financier'),
    ('BB', 'BC', 'Créances rattachées à des participations', ('267', '268'), ('2967', '2968'), 'financier'),
    ('BD', 'BE', 'Autres titres immobilisés', ('271', '272', '273'), ('2971', '2972', '2973'), 'financier'),
    ('BF', 'BG', 'Prêts', ('274',), ('2974',), 'financier'),
    ('BH', 'BI', 'Autres immobilisations financières', ('27', '275', '276'), ('297', '2975', '2976'), 'financier'),
    ('BL', 'BM', 'Matières premières, approvisionnements', ('31', '32'), ('391', '392'), 'circulant'),
    ('BN', 'BO', 'En cours de production de biens', ('33',), ('393',), 'circulant'),
    ('BP', 'BQ', 'En cours de production de services', ('34',), ('394',), 'circulant'),
    ('BR', 'BS', 'Produits intermédiaires et finis', ('35',), ('395',), 'circulant'),
    ('BT', 'BU', 'Marchandises', ('37',), ('397',), 'circulant'),
    ('BV', 'BW', 'Avances et acomptes versés sur commandes', ('4091',), (), 'circulant'),
    ('BX', 'BY', 'Clients et comptes rattachés', ('411', '413', '416', '417', '418'), ('491',), 'circulant'),
    ('BZ', 'CA', 'Autres créances', (), ('495', '496'), 'circulant'),
    ('CB', 'CC', 'Capital souscrit et appelé, non versé', ('4562',), (), 'circulant'),
    ('CD', 'CE', 'Valeurs mobilières de placement', ('50',), ('59',), 'circulant'),
    ('CF', 'CG', 'Disponibilités', ('51', '53', '54', '58'), (), 'circulant'),
    ('CH', 'CI', "Charges constatées d'avance", ('486',), (), 'circulant'),
    ('CW', None, "Frais d'émission d'emprunt à étaler", ('4816',), (), 'regul'),
    ('CM', None, 'Primes de remboursement des obligations', ('169',), (), 'regul'),
    ('CN', None, 'Écarts de conversion actif', ('476',), (), 'regul'),
]
ACTIF_TOTALS = {'incorporel': None, 'corporel': None}

PASSIF_2051 = [
    # (case, libellé, préfixes, rubrique)
    ('DA', 'Capital social ou individuel', ('101', '108'), 'capitaux'),
    ('DB', "Primes d'émission, de fusion, d'apport", ('104',), 'capitaux'),
    ('DC', 'Écarts de réévaluation', ('105',), 'capitaux'),
    ('DD', 'Réserve légale', ('1061',), 'capitaux'),
    ('DE', 'Réserves statutaires ou contractuelles', ('1063',), 'capitaux'),
    ('DF', 'Réserves réglementées', ('1062', '1064'), 'capitaux'),
    ('DG', 'Autres réserves', ('106', '1068'), 'capitaux'),
    ('DH', 'Report à nouveau', ('11',), 'capitaux'),
    ('DJ', "Subventions d'investissement", ('13',), 'capitaux'),
    ('DK', 'Provisions réglementées', ('14',), 'capitaux'),
    ('DM', 'Produit des émissions de titres participatifs', ('1671',), 'autres_fp'),
    ('DN', 'Avances conditionnées', ('1674',), 'autres_fp'),
    ('DP', 'Provisions pour risques', ('151',), 'provisions'),
    ('DQ', 'Provisions pour charges', ('15', '153', '155', '156', '157', '158'), 'provisions'),
    ('DS', 'Emprunts obligataires convertibles', ('161',), 'dettes'),
    ('DT', 'Autres emprunts obligataires', ('163',), 'dettes'),
    ('DU', 'Emprunts et dettes auprès des établissements de crédit', ('164', '519'), 'dettes'),
    ('DV', 'Emprunts et dettes financières divers', ('16', '165', '166', '1675', '168', '17', '455'), 'dettes'),
    ('DW', 'Avances et acomptes reçus sur commandes en cours', ('4191',), 'dettes'),
    ('DX', 'Dettes fournisseurs et comptes rattachés', ('401', '403', '408'), 'dettes'),
    ('DY', 'Dettes fiscales et sociales', ('42', '43', '44'), 'dettes'),
    ('DZ', 'Dettes sur immobilisations et comptes rattachés', ('404', '405'), 'dettes'),
    ('EA', 'Autres dettes', (), 'dettes'),
    ('EB', "Produits constatés d'avance", ('487',), 'dettes'),
    ('ED', 'Écarts de conversion passif', ('477',), 'regul'),
]

# Compte de résultat : (case, libellé, préfixes, sens P = produit / C = charge, groupe)
CR_LINES = [
    ('FC', 'Ventes de marchandises', ('707', '7097'), 'P', 'pe'),
    ('FF', 'Production vendue — biens', ('701', '702', '703', '7091', '7092', '7093'), 'P', 'pe'),
    ('FI', 'Production vendue — services', ('70', '704', '705', '706', '708', '709', '7094', '7095', '7096', '7098'), 'P', 'pe'),
    ('FM', 'Production stockée', ('71', '713'), 'P', 'pe'),
    ('FN', 'Production immobilisée', ('72',), 'P', 'pe'),
    ('FO', "Subventions d'exploitation", ('74',), 'P', 'pe'),
    ('FP', 'Reprises sur amortissements et provisions, transferts de charges', ('78', '781', '79', '791'), 'P', 'pe'),
    ('FQ', 'Autres produits', ('75',), 'P', 'pe'),
    ('FS', 'Achats de marchandises (y compris droits de douane)', ('607', '6087', '6097'), 'C', 'ce'),
    ('FT', 'Variation de stock (marchandises)', ('6037',), 'C', 'ce'),
    ('FU', 'Achats de matières premières et autres approvisionnements', ('601', '602', '6081', '6082', '6091', '6092'), 'C', 'ce'),
    ('FV', 'Variation de stock (matières premières et approvisionnements)', ('603', '6031', '6032'), 'C', 'ce'),
    ('FW', 'Autres achats et charges externes', ('60', '604', '605', '606', '6084', '6085', '6086', '6094', '6095', '6096', '61', '62'), 'C', 'ce'),
    ('FX', 'Impôts, taxes et versements assimilés', ('63',), 'C', 'ce'),
    ('FY', 'Salaires et traitements', ('64', '641', '644', '648'), 'C', 'ce'),
    ('FZ', 'Charges sociales', ('645', '646', '647'), 'C', 'ce'),
    ('GA', 'Dotations aux amortissements sur immobilisations', ('68', '681', '6811', '6812'), 'C', 'ce'),
    ('GB', 'Dotations aux dépréciations sur immobilisations', ('6816',), 'C', 'ce'),
    ('GC', 'Dotations aux dépréciations sur actif circulant', ('6817',), 'C', 'ce'),
    ('GD', 'Dotations aux provisions pour risques et charges', ('6815',), 'C', 'ce'),
    ('GE', 'Autres charges', ('65',), 'C', 'ce'),
    ('GH', 'Bénéfice attribué ou perte transférée (opérations en commun)', ('755',), 'P', 'oc'),
    ('GI', 'Perte supportée ou bénéfice transféré (opérations en commun)', ('655',), 'C', 'oc'),
    ('GJ', 'Produits financiers de participations', ('761',), 'P', 'pf'),
    ('GK', "Produits des autres valeurs mobilières et créances de l'actif immobilisé", ('762',), 'P', 'pf'),
    ('GL', 'Autres intérêts et produits assimilés', ('76', '763', '764', '765', '768'), 'P', 'pf'),
    ('GM', 'Reprises sur provisions et transferts de charges (financier)', ('786', '796'), 'P', 'pf'),
    ('GN', 'Différences positives de change', ('766',), 'P', 'pf'),
    ('GO', 'Produits nets sur cessions de valeurs mobilières de placement', ('767',), 'P', 'pf'),
    ('GQ', 'Dotations financières aux amortissements et provisions', ('686',), 'C', 'cf'),
    ('GR', 'Intérêts et charges assimilées', ('66', '661', '664', '665', '668'), 'C', 'cf'),
    ('GS', 'Différences négatives de change', ('666',), 'C', 'cf'),
    ('GT', 'Charges nettes sur cessions de valeurs mobilières de placement', ('667',), 'C', 'cf'),
    ('HA', 'Produits exceptionnels sur opérations de gestion', ('77', '771'), 'P', 'px'),
    ('HB', 'Produits exceptionnels sur opérations en capital', ('775', '777', '778'), 'P', 'px'),
    ('HC', 'Reprises sur provisions et transferts de charges (exceptionnel)', ('787', '797'), 'P', 'px'),
    ('HE', 'Charges exceptionnelles sur opérations de gestion', ('67', '671'), 'C', 'cx'),
    ('HF', 'Charges exceptionnelles sur opérations en capital', ('675', '678'), 'C', 'cx'),
    ('HG', 'Dotations exceptionnelles aux amortissements et provisions', ('687',), 'C', 'cx'),
    ('HJ', 'Participation des salariés aux résultats', ('691',), 'C', 'is'),
    ('HK', 'Impôts sur les bénéfices', ('69', '695', '696', '697', '698', '699'), 'C', 'is'),
]

# 2054 / 2055 : rubriques d'immobilisations (préfixes des valeurs brutes et des amortissements)
IMMO_RUBRIQUES = [
    ('Immobilisations incorporelles', ('20',), ('280',)),
    ('Terrains', ('211', '212'), ('2811', '2812')),
    ('Constructions', ('213', '214'), ('2813', '2814')),
    ('Installations techniques, matériel et outillage', ('215',), ('2815',)),
    ('Autres immobilisations corporelles', ('218', '22'), ('2818', '282')),
    ('Immobilisations corporelles en cours, avances et acomptes', ('23',), ()),
    ('Participations et autres titres', ('26', '271', '272', '273'), ()),
    ('Prêts et autres immobilisations financières', ('274', '275', '276', '27'), ()),
]
# 2056 : provisions et dépréciations (préfixes ; dotations = mouvements crédit, reprises = débit)
PROVISION_RUBRIQUES = [
    ('Provisions réglementées', ('14',)),
    ('Provisions pour risques et charges', ('15',)),
    ('Dépréciations des immobilisations', ('29',)),
    ('Dépréciations des stocks et en-cours', ('39',)),
    ('Dépréciations des comptes clients', ('491',)),
    ('Autres dépréciations', ('495', '496', '59')),
]


def _match(code, prefixes):
    return max((len(p) for p in prefixes if code.startswith(p)), default=0)


def _r(x):
    return round(float(x or 0), 2)


def _balances(conn, start, end, entity_id):
    """Par compte : ouverture (à-nouveaux du premier jour compris), débit, crédit
    (hors à-nouveaux) et solde de clôture (débit − crédit)."""
    tb = trial_balance(conn, start, end, entity_id, 'generale')
    ef = 'e.entity_id=?' if entity_id else 'e.entity_id IS NULL'
    ep = (entity_id,) if entity_id else ()
    an = {r['account_code']: (float(r['d'] or 0), float(r['c'] or 0)) for r in conn.execute(
        f"""SELECT l.account_code, SUM(l.debit) d, SUM(l.credit) c FROM accounting_entry_lines l
            JOIN accounting_entries e ON e.id=l.entry_id
            WHERE e.journal_code='AN' AND e.entry_date=? AND {ef} GROUP BY l.account_code""",
        (start.isoformat(), *ep)).fetchall()}
    out = {}
    for l in tb['lines']:
        code = l['key']
        ad, ac = an.get(code, (0.0, 0.0))
        opening = l['opening_debit'] - l['opening_credit'] + ad - ac
        debit, credit = l['debit'] - ad, l['credit'] - ac
        out[code] = {'opening': _r(opening), 'debit': _r(debit), 'credit': _r(credit),
                     'closing': _r(l['closing_debit'] - l['closing_credit'])}
    return out


def _compte_resultat(bal):
    lines = {box: 0.0 for box, *_ in CR_LINES}
    meta = {box: (label, sens, grp) for box, label, _p, sens, grp in CR_LINES}
    unmatched = []
    for code, b in bal.items():
        if code[0] not in '67' or abs(b['closing']) < 0.005:
            continue
        best, blen = None, 0
        for box, _l, prefixes, _s, _g in CR_LINES:
            m = _match(code, prefixes)
            if m > blen:
                best, blen = box, m
        if best is None:
            best = 'FQ' if code[0] == '7' else 'GE'
            unmatched.append(code)
        sens = meta[best][1]
        lines[best] += -b['closing'] if sens == 'P' else b['closing']
    lines = {k: _r(v) for k, v in lines.items()}
    g = lambda grp: _r(sum(lines[k] for k in meta if meta[k][2] == grp))
    pe, ce = g('pe'), g('ce')
    lines.update({
        'FA': lines['FC'], 'FB': 0.0, 'FD': lines['FF'], 'FE': 0.0, 'FG': lines['FI'], 'FH': 0.0,
        'FJ': _r(lines['FC'] + lines['FF'] + lines['FI']), 'FK': 0.0,
    })
    lines['FL'] = lines['FJ']
    lines['FR'] = pe
    lines['GF'] = ce
    lines['GG'] = _r(pe - ce)
    lines['GP'] = g('pf')
    lines['GU'] = g('cf')
    lines['GV'] = _r(lines['GP'] - lines['GU'])
    lines['GW'] = _r(lines['GG'] + lines['GH'] - lines['GI'] + lines['GV'])
    lines['HD'] = g('px')
    lines['HH'] = g('cx')
    lines['HI'] = _r(lines['HD'] - lines['HH'])
    lines['HL'] = _r(pe + lines['GH'] + lines['GP'] + lines['HD'])
    lines['HM'] = _r(ce + lines['GI'] + lines['GU'] + lines['HH'] + lines['HJ'] + lines['HK'])
    lines['HN'] = _r(lines['HL'] - lines['HM'])
    return lines, unmatched


def _bilan(bal, resultat):
    actif = {row[0]: {'brut': 0.0, 'amort': 0.0} for row in ACTIF_2050}
    passif = {row[0]: 0.0 for row in PASSIF_2051}
    prior_result = 0.0
    unassigned = []
    for code, b in bal.items():
        v = b['closing']
        if code[0] not in '12345' or abs(v) < 0.005:
            continue
        if code.startswith('12'):          # résultat clos non encore affecté
            prior_result += -v
            continue
        best = (0, None, None)
        for box, _a, _lab, brut_p, amort_p, _rub in ACTIF_2050:
            for kind, prefixes in (('brut', brut_p), ('amort', amort_p)):
                m = _match(code, prefixes)
                if m > best[0]:
                    best = (m, ('A', box), kind)
        for box, _lab, prefixes, _rub in PASSIF_2051:
            m = _match(code, prefixes)
            if m > best[0]:
                best = (m, ('P', box), 'passif')
        target, kind = best[1], best[2]
        # Reclassements selon le sens du solde.
        if target == ('A', 'CF') and v < 0:
            target, kind = ('P', 'DU'), 'passif'               # banque créditrice : concours bancaires
        elif target and target[0] == 'P' and code[0] == '4' and v > 0:
            target, kind = ('A', 'BZ'), 'brut'                 # tiers débiteur : autre créance
        elif target and target[0] == 'A' and kind == 'brut' and code[0] == '4' and v < 0:
            target, kind = ('P', 'EA'), 'passif'               # client / acompte créditeur : autre dette
        if target is None:
            if code[0] == '4':
                target, kind = (('A', 'BZ'), 'brut') if v > 0 else (('P', 'EA'), 'passif')
            else:
                unassigned.append(code)
                continue
        if target[0] == 'A':
            actif[target[1]]['amort' if kind == 'amort' else 'brut'] += (-v if kind == 'amort' else v)
        else:
            passif[target[1]] += -v
    rows_a = []
    for box, amort_box, label, _b, _a, rub in ACTIF_2050:
        brut, amort = _r(actif[box]['brut']), _r(actif[box]['amort'])
        rows_a.append({'box_brut': box, 'box_amort': amort_box, 'label': label, 'rubrique': rub,
                       'brut': brut, 'amort': amort, 'net': _r(brut - amort)})
    sub = lambda rubs: (_r(sum(r['brut'] for r in rows_a if r['rubrique'] in rubs)),
                        _r(sum(r['amort'] for r in rows_a if r['rubrique'] in rubs)))
    tot_immo = sub(('incorporel', 'corporel', 'financier'))
    tot_circ = sub(('circulant',))
    tot_all = sub(('capital', 'incorporel', 'corporel', 'financier', 'circulant', 'regul'))
    totals_a = {
        'BJ': tot_immo[0], 'BK': tot_immo[1], 'BJ_net': _r(tot_immo[0] - tot_immo[1]),
        'CJ': tot_circ[0], 'CK': tot_circ[1], 'CJ_net': _r(tot_circ[0] - tot_circ[1]),
        'CO': tot_all[0], '1A': tot_all[1], 'CO_net': _r(tot_all[0] - tot_all[1]),
    }
    passif = {k: _r(v) for k, v in passif.items()}
    # Résultat d'un exercice clos non encore affecté (compte 12) : présenté en report à
    # nouveau pour que la case DI reste égale au résultat de l'exercice (HN).
    passif['DH'] = _r(passif['DH'] + prior_result)
    passif['DI'] = _r(resultat)
    rub = {row[0]: row[3] for row in PASSIF_2051}
    rub['DI'] = 'capitaux'
    s = lambda name: _r(sum(v for k, v in passif.items() if rub.get(k) == name))
    totals_p = {'DL': s('capitaux'), 'DO': s('autres_fp'), 'DR': s('provisions'), 'EC': s('dettes')}
    totals_p['EE'] = _r(totals_p['DL'] + totals_p['DO'] + totals_p['DR'] + totals_p['EC'] + passif['ED'])
    rows_p = [{'box': box, 'label': label, 'rubrique': r, 'amount': passif[box]} for box, label, _p, r in PASSIF_2051]
    rows_p.insert(next(i for i, x in enumerate(rows_p) if x['box'] == 'DJ'),
                  {'box': 'DI', 'label': "Résultat de l'exercice (bénéfice ou perte)", 'rubrique': 'capitaux',
                   'amount': passif['DI']})
    return rows_a, totals_a, rows_p, totals_p, unassigned, _r(prior_result)


def _rubrique_movements(bal, prefixes, exclude=()):
    o = d = c = 0.0
    for code, b in bal.items():
        if _match(code, prefixes) and not any(code.startswith(x) for x in exclude):
            o += b['opening']; d += b['debit']; c += b['credit']
    return _r(o), _r(d), _r(c)


def _immobilisations(bal):
    """2054 (valeurs brutes) et 2055 (amortissements) par rubrique."""
    immo, amort = [], []
    for label, brut_p, amort_p in IMMO_RUBRIQUES:
        o, d, c = _rubrique_movements(bal, brut_p, exclude=('28', '29'))
        immo.append({'label': label, 'debut': o, 'augmentations': d, 'diminutions': c, 'fin': _r(o + d - c)})
        if amort_p:
            o, d, c = _rubrique_movements(bal, amort_p)
            # Comptes d'amortissement créditeurs : dotations au crédit, reprises au débit.
            amort.append({'label': label, 'debut': _r(-o), 'dotations': c, 'diminutions': d, 'fin': _r(-o + c - d)})
    tot = lambda rows, keys: {k: _r(sum(r[k] for r in rows)) for k in keys}
    return (immo, tot(immo, ('debut', 'augmentations', 'diminutions', 'fin')),
            amort, tot(amort, ('debut', 'dotations', 'diminutions', 'fin')))


def _provisions(bal):
    rows = []
    for label, prefixes in PROVISION_RUBRIQUES:
        o, d, c = _rubrique_movements(bal, prefixes)
        rows.append({'label': label, 'debut': _r(-o), 'dotations': c, 'reprises': d, 'fin': _r(-o + c - d)})
    return rows, {k: _r(sum(r[k] for r in rows)) for k in ('debut', 'dotations', 'reprises', 'fin')}


def _loan_split(conn, end, entity_id):
    """Capital restant dû des emprunts (échéancier) découpé à 1 an / 1 à 5 ans / + 5 ans
    après la clôture."""
    ef = 'l.entity_id=?' if entity_id else 'l.entity_id IS NULL'
    ep = (entity_id,) if entity_id else ()
    try:
        rows = conn.execute(
            f"""SELECT i.due_date, i.capital_amount FROM loan_installments i JOIN loans l ON l.id=i.loan_id
                WHERE {ef} AND i.due_date>?""", (*ep, end.isoformat())).fetchall()
    except Exception:
        return None
    one, five = end + timedelta(days=365), end + timedelta(days=5 * 365)
    split = [0.0, 0.0, 0.0]
    for r in rows:
        d = str(r['due_date'])[:10]
        amt = float(r['capital_amount'] or 0)
        idx = 0 if d <= one.isoformat() else (1 if d <= five.isoformat() else 2)
        split[idx] += amt
    return [_r(x) for x in split]


CREANCES_2057 = [
    ('UP', 'Prêts', ('274',), 'plus'),
    ('UT', 'Autres immobilisations financières', ('275', '276'), 'plus'),
    ('VA', 'Clients douteux ou litigieux', ('416',), 'un'),
    ('UX', 'Autres créances clients', ('411', '413', '417', '418'), 'un'),
    ('UY', 'Personnel et comptes rattachés', ('425',), 'un'),
    ('UZ', 'Sécurité sociale et autres organismes sociaux', ('43',), 'un'),
    ('VM', 'Impôts sur les bénéfices', ('444',), 'un'),
    ('VB', 'Taxe sur la valeur ajoutée', ('445',), 'un'),
    ('VN', 'Autres impôts, taxes et versements assimilés', ('447', '448'), 'un'),
    ('VP', 'Divers', ('46',), 'un'),
    ('VC', 'Groupe et associés', ('455', '451'), 'un'),
    ('VR', 'Débiteurs divers', ('467', '4',), 'un'),
    ('VS', "Charges constatées d'avance", ('486',), 'un'),
]
DETTES_2057 = [
    ('7Y', 'Emprunts obligataires convertibles', ('161',)),
    ('7Z', 'Autres emprunts obligataires', ('163',)),
    ('VG', "Emprunts auprès des établissements de crédit à 1 an maximum à l'origine", ('519', '512')),
    ('VH', "Emprunts auprès des établissements de crédit à plus d'1 an à l'origine", ('164',)),
    ('8A', 'Emprunts et dettes financières divers', ('165', '166', '168', '17')),
    ('8B', 'Fournisseurs et comptes rattachés', ('401', '403', '408')),
    ('8C', 'Personnel et comptes rattachés', ('421', '422', '427', '428')),
    ('8D', 'Sécurité sociale et autres organismes sociaux', ('43',)),
    ('8E', 'Impôts sur les bénéfices', ('444',)),
    ('VW', 'Taxe sur la valeur ajoutée', ('445',)),
    ('VQ', 'Autres impôts, taxes et assimilés', ('447', '448')),
    ('8J', 'Dettes sur immobilisations et comptes rattachés', ('404', '405')),
    ('VI', 'Groupe et associés', ('455', '451')),
    ('8K', 'Autres dettes', ('4',)),
    ('8L', "Produits constatés d'avance", ('487',)),
]


def _echeances(conn, bal, end, entity_id):
    cre = {box: 0.0 for box, *_ in CREANCES_2057}
    det = {box: 0.0 for box, *_ in DETTES_2057}
    for code, b in bal.items():
        v = b['closing']
        if abs(v) < 0.005 or code[0] not in '12345':
            continue
        if code[0] == '1' and not (v < 0 and code.startswith(('16', '17'))):
            continue
        if code.startswith(('28', '29', '39', '49', '59', '409', '419', '4562', '4816', '476', '477')):
            continue
        if v > 0:
            if code[0] == '5':
                continue
            best, blen = None, 0
            for box, _l, prefixes, _w in CREANCES_2057:
                m = _match(code, prefixes)
                if m > blen:
                    best, blen = box, m
            if best and (code[0] == '4' or best in ('UP', 'UT')):
                cre[best] += v
        else:
            if code[0] == '5' and not code.startswith(('512', '519')):
                continue
            if code[0] in '23':
                continue
            best, blen = None, 0
            for box, _l, prefixes in DETTES_2057:
                m = _match(code, prefixes)
                if m > blen:
                    best, blen = box, m
            if best:
                det[best] += -v
    creances = []
    for box, label, _p, when in CREANCES_2057:
        amt = _r(cre[box])
        creances.append({'box': box, 'label': label, 'brut': amt,
                         'un_an': amt if when == 'un' else 0.0, 'plus_un_an': amt if when == 'plus' else 0.0})
    loans = _loan_split(conn, end, entity_id)
    dettes = []
    for box, label, _p in DETTES_2057:
        amt = _r(det[box])
        split = [amt, 0.0, 0.0]
        if box == 'VH' and loans and amt:
            sched = sum(loans)
            if sched > 0:
                # Échéancier proportionné au solde comptable (écart = intérêts courus, etc.).
                split = [_r(amt * x / sched) for x in loans]
                split[0] = _r(amt - split[1] - split[2])
        dettes.append({'box': box, 'label': label, 'brut': amt, 'un_an': split[0], 'un_cinq': split[1], 'plus_cinq': split[2]})
    return creances, dettes


def _sum(bal, prefixes, field='closing', sign=1):
    return _r(sign * sum(b[field] for code, b in bal.items() if _match(code, prefixes)))


def valeur_ajoutee(bal):
    """2059-E : valeur ajoutée produite (base de la CVAE), selon les rubriques du formulaire."""
    p = lambda *pr: _sum(bal, pr, sign=-1)     # produits : solde créditeur
    c = lambda *pr: _sum(bal, pr)              # charges : solde débiteur
    lines = {
        'OA': p('70'),
        'OK': p('751'),
        'OL': 0.0,
        'OT': p('791'),
        'OH': _r(p('75') - p('751') - p('755')),
        'OE': p('72'),
        'OF': p('74'),
        'OD': max(0.0, p('713')),
        'OI': 0.0,
        'XT': p('7714'),
        'ON': _r(c('601', '602', '604', '605', '606', '607', '608', '609')),
        'OQ': _r(max(0.0, c('603')) + max(0.0, -p('713'))),
        'OR': _r(c('61', '62') - c('612', '613')),
        # Loyers déductibles = seulement ceux des locations de 6 mois au plus (hors crédit-bail) :
        # non identifiables en comptabilité, laissés à compléter (VA légèrement majorée).
        'OS': 0.0,
        # Taxes sur le chiffre d'affaires et contributions indirectes (pas les impôts directs).
        'OZ': c('6352', '6353'),
        'OW': _r(c('65') - c('655')),
        'OU': 0.0,
        'OY': 0.0,
    }
    lines['OX'] = _r(lines['OA'] + lines['OK'] + lines['OL'] + lines['OT'])
    lines['OM'] = _r(lines['OH'] + lines['OE'] + lines['OF'] + lines['OD'] + lines['OI'] + lines['XT'])
    lines['OJ'] = _r(lines['ON'] + lines['OQ'] + lines['OR'] + lines['OS'] + lines['OZ'] + lines['OW'] + lines['OU'] + lines['OY'])
    lines['OG'] = _r(lines['OX'] + lines['OM'] - lines['OJ'])
    return lines


VA_LABELS = {
    'OA': 'Ventes de produits fabriqués, prestations de services et marchandises',
    'OK': 'Redevances pour concessions, brevets, licences',
    'OL': "Plus-values de cession d'immobilisations (activité normale et courante)",
    'OT': 'Refacturations de frais (transferts de charges)',
    'OX': "Total 1 — chiffre d'affaires de référence",
    'OH': 'Autres produits de gestion courante',
    'OE': 'Production immobilisée',
    'OF': "Subventions d'exploitation reçues",
    'OD': 'Variation positive des stocks',
    'OI': 'Transferts de charges déductibles de la valeur ajoutée',
    'XT': 'Rentrées sur créances amorties',
    'OM': 'Total 2 — autres produits',
    'ON': 'Achats',
    'OQ': 'Variation négative des stocks',
    'OR': 'Services extérieurs (hors loyers et redevances)',
    'OS': 'Loyers et redevances déductibles (locations de 6 mois au plus, hors crédit-bail) — à compléter',
    'OZ': 'Taxes déductibles de la valeur ajoutée',
    'OW': 'Autres charges de gestion courante',
    'OU': 'Charges déductibles afférentes à la production immobilisée',
    'OY': "Moins-values de cession (activité normale et courante)",
    'OJ': 'Total 3 — charges',
    'OG': 'Valeur ajoutée produite (total 1 + total 2 − total 3)',
}


def deficit_imputable(deficits_anterieurs, benefice):
    """Imputation des déficits antérieurs (art. 209 I CGI) : dans la limite d'1 M€
    majoré de 50 % du bénéfice excédant 1 M€."""
    benefice = max(0.0, float(benefice or 0))
    stock = max(0.0, float(deficits_anterieurs or 0))
    plafond = benefice if benefice <= 1_000_000 else 1_000_000 + 0.5 * (benefice - 1_000_000)
    return _r(min(stock, plafond))


def resultat_fiscal(cr, bal, settings=None):
    """2058-A et 2058-B. settings : réintégrations / déductions saisies (liste de
    {'code','label','amount'}), déficits restant à reporter de l'exercice précédent."""
    settings = settings or {}
    resultat = cr['HN']
    reint = [
        {'code': 'WA', 'label': "Bénéfice comptable de l'exercice", 'amount': max(0.0, resultat), 'auto': True},
        {'code': 'I7', 'label': 'Impôt sur les sociétés (charge comptabilisée)', 'amount': _sum(bal, ('695', '696')), 'auto': True},
        {'code': 'WJ', 'label': 'Amendes et pénalités', 'amount': _sum(bal, ('6712',)), 'auto': True},
    ]
    for r in settings.get('reintegrations') or []:
        if float(r.get('amount') or 0):
            reint.append({'code': (r.get('code') or '').upper()[:3], 'label': r.get('label') or 'Réintégration',
                          'amount': _r(r['amount']), 'auto': False})
    deduc = [{'code': 'WS', 'label': "Perte comptable de l'exercice", 'amount': max(0.0, -resultat), 'auto': True}]
    for r in settings.get('deductions') or []:
        if float(r.get('amount') or 0):
            deduc.append({'code': (r.get('code') or '').upper()[:3], 'label': r.get('label') or 'Déduction',
                          'amount': _r(r['amount']), 'auto': False})
    reint = [dict(x, amount=_r(x['amount'])) for x in reint]
    deduc = [dict(x, amount=_r(x['amount'])) for x in deduc]
    wr = _r(sum(x['amount'] for x in reint))
    xh = _r(sum(x['amount'] for x in deduc))
    xi = _r(max(0.0, wr - xh))
    xj = _r(max(0.0, xh - wr))
    k4 = _r(settings.get('deficits_anterieurs') or 0)
    xl = deficit_imputable(k4, xi)
    xn = _r(xi - xl)
    xo = xj
    k5 = xl
    k6 = _r(k4 - k5)
    return {
        'reintegrations': reint, 'deductions': deduc, 'WR': wr, 'XH': xh, 'XI': xi, 'XJ': xj,
        'ZL': 0.0, 'XL': xl, 'XN': xn, 'XO': xo,
        '2058B': {'K4': k4, 'K5': k5, 'K6': k6, 'YJ': xo, 'YK': _r(k6 + xo)},
    }


def renseignements_2058c(bal, cr):
    c = lambda *pr: _sum(bal, pr)
    tva = _r(sum(b['credit'] - b['debit'] for code, b in bal.items() if code.startswith('4457')))
    return [
        {'code': 'YQ', 'label': 'Engagements de crédit-bail mobilier', 'amount': 0.0},
        {'code': 'YT', 'label': 'Sous-traitance', 'amount': c('611')},
        {'code': 'XQ', 'label': 'Locations, charges locatives et de copropriété', 'amount': c('613', '614')},
        {'code': 'YU', 'label': "Personnel extérieur à l'entreprise", 'amount': c('621')},
        {'code': 'SS', 'label': "Rémunérations d'intermédiaires et honoraires", 'amount': c('622')},
        {'code': 'ZJ', 'label': 'Total du poste FW (autres achats et charges externes)', 'amount': cr['FW']},
        {'code': 'YW', 'label': 'CFE et CVAE', 'amount': c('63511')},
        {'code': 'ZS', 'label': 'Autres impôts, taxes et versements assimilés', 'amount': _r(cr['FX'] - c('63511'))},
        {'code': 'YY', 'label': 'Montant de la TVA collectée', 'amount': _r(max(0.0, tva))},
        {'code': 'ØB', 'label': 'Montant brut des salaires', 'amount': cr['FY']},
    ]


def liasse_normale(conn, fy_start, fy_end, entity_id=None, settings=None, previous=None):
    """Calcule toute la liasse. previous = (start, end) de l'exercice précédent pour la
    colonne N-1 du bilan et du compte de résultat."""
    if isinstance(fy_start, str):
        fy_start = date.fromisoformat(fy_start)
    if isinstance(fy_end, str):
        fy_end = date.fromisoformat(fy_end)
    bal = _balances(conn, fy_start, fy_end, entity_id)
    cr, unmatched = _compte_resultat(bal)
    actif, totals_a, passif, totals_p, unassigned, prior_unallocated = _bilan(bal, cr['HN'])
    immo, immo_tot, amort, amort_tot = _immobilisations(bal)
    prov, prov_tot = _provisions(bal)
    creances, dettes = _echeances(conn, bal, fy_end, entity_id)
    fiscal = resultat_fiscal(cr, bal, settings)
    va = valeur_ajoutee(bal)
    prev = None
    if previous:
        pbal = _balances(conn, previous[0], previous[1], entity_id)
        pcr, _u = _compte_resultat(pbal)
        pa, pta, pp, ptp, _x, _pr = _bilan(pbal, pcr['HN'])
        prev = {'cr': pcr, 'actif': {a['box_brut']: a['net'] for a in pa}, 'totals_a': pta,
                'passif': {p['box']: p['amount'] for p in pp}, 'totals_p': ptp}
    checks = []
    if abs(totals_a['CO_net'] - totals_p['EE']) >= 1:
        checks.append(f"Bilan déséquilibré : actif {totals_a['CO_net']:,.0f} € / passif {totals_p['EE']:,.0f} €.".replace(',', ' '))
    if unassigned:
        checks.append('Comptes de bilan non rattachés à une case : ' + ', '.join(sorted(unassigned)) + '.')
    if prior_unallocated:
        checks.append(f"Résultat d'un exercice antérieur non encore affecté (compte 12) : {prior_unallocated:,.0f} € présentés "
                      "en report à nouveau (DH). Passez l'écriture d'affectation décidée par l'assemblée.".replace(',', ' '))
    if unmatched:
        checks.append('Comptes de gestion rattachés par défaut (autres produits / autres charges) : '
                      + ', '.join(sorted(unmatched)) + '.')
    duration = (fy_end - fy_start).days + 1
    return {
        'start': fy_start, 'end': fy_end, 'duration_days': duration,
        'actif': actif, 'totals_a': totals_a, 'passif': passif, 'totals_p': totals_p,
        'cr': cr, 'cr_labels': {box: label for box, label, *_ in CR_LINES},
        'immobilisations': immo, 'immo_tot': immo_tot, 'amortissements': amort, 'amort_tot': amort_tot,
        'provisions': prov, 'prov_tot': prov_tot, 'creances': creances, 'dettes': dettes,
        'fiscal': fiscal, 'renseignements': renseignements_2058c(bal, cr),
        'va': va, 'va_labels': VA_LABELS, 'previous': prev,
        'balanced': abs(totals_a['CO_net'] - totals_p['EE']) < 1, 'checks': checks,
        'chiffre_affaires': cr['FL'],
    }


def liasse_normale_rows(l):
    """Export à plat (formulaire, case, libellé, montant arrondi à l'euro) — format
    réutilisable pour une saisie dans l'outil du partenaire EDI ou du cabinet."""
    e = lambda x: int(round(float(x or 0)))
    rows = []
    for a in l['actif']:
        rows.append({'Formulaire': '2050', 'Case': a['box_brut'], 'Libellé': f"{a['label']} — brut", 'Montant': e(a['brut'])})
        if a['box_amort']:
            rows.append({'Formulaire': '2050', 'Case': a['box_amort'], 'Libellé': f"{a['label']} — amortissements, dépréciations",
                         'Montant': e(a['amort'])})
    t = l['totals_a']
    for box, label in (('BJ', 'Total II — actif immobilisé (brut)'), ('BK', 'Total II — amortissements'),
                       ('CJ', 'Total III — actif circulant (brut)'), ('CK', 'Total III — dépréciations'),
                       ('CO', 'Total général (brut)'), ('1A', 'Total général — amortissements')):
        rows.append({'Formulaire': '2050', 'Case': box, 'Libellé': label, 'Montant': e(t[box])})
    for p in l['passif']:
        rows.append({'Formulaire': '2051', 'Case': p['box'], 'Libellé': p['label'], 'Montant': e(p['amount'])})
    for box, label in (('DL', 'Total I — capitaux propres'), ('DO', 'Total II — autres fonds propres'),
                       ('DR', 'Total III — provisions'), ('EC', 'Total IV — dettes'), ('EE', 'Total général')):
        rows.append({'Formulaire': '2051', 'Case': box, 'Libellé': label, 'Montant': e(l['totals_p'][box])})
    cr = l['cr']
    order_2052 = ['FA', 'FB', 'FC', 'FD', 'FE', 'FF', 'FG', 'FH', 'FI', 'FJ', 'FK', 'FL', 'FM', 'FN', 'FO', 'FP', 'FQ', 'FR',
                  'FS', 'FT', 'FU', 'FV', 'FW', 'FX', 'FY', 'FZ', 'GA', 'GB', 'GC', 'GD', 'GE', 'GF', 'GG', 'GH', 'GI',
                  'GJ', 'GK', 'GL', 'GM', 'GN', 'GO', 'GP', 'GQ', 'GR', 'GS', 'GT', 'GU', 'GV', 'GW']
    order_2053 = ['HA', 'HB', 'HC', 'HD', 'HE', 'HF', 'HG', 'HH', 'HI', 'HJ', 'HK', 'HL', 'HM', 'HN']
    for form, order in (('2052', order_2052), ('2053', order_2053)):
        for box in order:
            rows.append({'Formulaire': form, 'Case': box, 'Libellé': CR_TOTAL_LABELS.get(box) or l['cr_labels'].get(box, ''),
                         'Montant': e(cr[box])})
    f = l['fiscal']
    for x in f['reintegrations']:
        rows.append({'Formulaire': '2058-A', 'Case': x['code'], 'Libellé': x['label'], 'Montant': e(x['amount'])})
    rows.append({'Formulaire': '2058-A', 'Case': 'WR', 'Libellé': 'Total I — réintégrations', 'Montant': e(f['WR'])})
    for x in f['deductions']:
        rows.append({'Formulaire': '2058-A', 'Case': x['code'], 'Libellé': x['label'], 'Montant': e(x['amount'])})
    for box, label in (('XH', 'Total II — déductions'), ('XI', 'Bénéfice (I − II)'), ('XJ', 'Déficit (II − I)'),
                       ('XL', 'Déficits antérieurs imputés'), ('XN', 'Résultat fiscal bénéficiaire'),
                       ('XO', 'Déficit reportable en avant')):
        rows.append({'Formulaire': '2058-A', 'Case': box, 'Libellé': label, 'Montant': e(f[box])})
    for box, label in (('K4', "Déficits restant à reporter au titre de l'exercice précédent"), ('K5', 'Déficits imputés'),
                       ('K6', 'Déficits reportables'), ('YJ', "Déficit de l'exercice"), ('YK', 'Total des déficits restant à reporter')):
        rows.append({'Formulaire': '2058-B', 'Case': box, 'Libellé': label, 'Montant': e(f['2058B'][box])})
    for x in l['renseignements']:
        rows.append({'Formulaire': '2058-C', 'Case': x['code'], 'Libellé': x['label'], 'Montant': e(x['amount'])})
    for c in l['creances']:
        rows.append({'Formulaire': '2057', 'Case': c['box'], 'Libellé': f"Créances — {c['label']}", 'Montant': e(c['brut'])})
    for d in l['dettes']:
        rows.append({'Formulaire': '2057', 'Case': d['box'], 'Libellé': f"Dettes — {d['label']}", 'Montant': e(d['brut'])})
    for box in ('OA', 'OK', 'OL', 'OT', 'OX', 'OH', 'OE', 'OF', 'OD', 'OI', 'XT', 'OM', 'ON', 'OQ', 'OR', 'OS', 'OZ',
                'OW', 'OU', 'OY', 'OJ', 'OG'):
        rows.append({'Formulaire': '2059-E', 'Case': box, 'Libellé': VA_LABELS[box], 'Montant': e(l['va'][box])})
    return rows


CR_TOTAL_LABELS = {
    'FA': 'Ventes de marchandises — France', 'FB': 'Ventes de marchandises — exportations et livraisons intracommunautaires',
    'FD': 'Production vendue (biens) — France', 'FE': 'Production vendue (biens) — exportations',
    'FG': 'Production vendue (services) — France', 'FH': 'Production vendue (services) — exportations',
    'FJ': "Chiffre d'affaires net — France", 'FK': "Chiffre d'affaires net — exportations", 'FL': "Chiffre d'affaires net — total",
    'FR': "Total des produits d'exploitation (I)", 'GF': "Total des charges d'exploitation (II)",
    'GG': "1 — Résultat d'exploitation (I − II)", 'GP': 'Total des produits financiers (V)',
    'GU': 'Total des charges financières (VI)', 'GV': '2 — Résultat financier (V − VI)',
    'GW': '3 — Résultat courant avant impôts', 'HD': 'Total des produits exceptionnels (VII)',
    'HH': 'Total des charges exceptionnelles (VIII)', 'HI': '4 — Résultat exceptionnel (VII − VIII)',
    'HL': 'Total des produits', 'HM': 'Total des charges', 'HN': '5 — Bénéfice ou perte',
}

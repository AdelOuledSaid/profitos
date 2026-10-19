"""Impôt sur les sociétés (2065, acomptes 2571, relevé de solde 2572) et CVAE (1330-CVAE,
acomptes 1329-AC, solde 1329-DEF), calculés à partir de la liasse du régime normal.

Règles en vigueur pour les exercices 2026 (loi n° 2026-103 du 19 février 2026) :
- IS : taux normal 25 % ; taux réduit 15 % jusqu'à 42 500 € de bénéfice par période de
  12 mois pour les PME (CA HT ≤ 10 M€, capital entièrement libéré, détenu à 75 % au moins
  par des personnes physiques) ; contribution sociale de 3,3 % sur l'IS excédant
  763 000 € (exonération des PME au CA < 7,63 M€) ;
- acomptes d'IS les 15 mars, 15 juin, 15 septembre et 15 décembre quand l'IS de
  référence dépasse 3 000 € ; solde le 15 du 4e mois suivant la clôture ;
- CVAE en cours de suppression : taux maximal 0,28 % en 2026 et 2027, 0,19 % en 2028,
  0,09 % en 2029, suppression en 2030 ; non due sous 63 € ; valeur ajoutée plafonnée à
  80 % du CA (85 % au-delà de 7,6 M€) ; taxe additionnelle CCI 9,23 % (2026-2027).

Résultat indicatif à faire valider : les crédits d'impôt, l'intégration fiscale, les
régimes particuliers et le carry-back ne sont pas calculés automatiquement.
"""
from datetime import date, timedelta

IS_NORMAL_RATE = 0.25
IS_REDUCED_RATE = 0.15
IS_REDUCED_CEILING = 42_500
IS_PME_TURNOVER_MAX = 10_000_000
SOCIAL_CONTRIBUTION_RATE = 0.033
SOCIAL_CONTRIBUTION_ALLOWANCE = 763_000
SOCIAL_CONTRIBUTION_PME_TURNOVER = 7_630_000
IS_INSTALMENT_THRESHOLD = 3_000

# CVAE : (taux plafond, (seuils de la formule)) par année d'imposition.
# Formule légale (art. 1586 ter CGI) : taux progressif selon le CA, linéaire par tranche.
CVAE_SCHEDULE = {
    # année : (taux tranche 500 k€–3 M€, taux additionnel 3–10 M€, taux additionnel 10–50 M€, taux maximal)
    2026: (0.00094, 0.00169, 0.00019, 0.0028),
    2027: (0.00094, 0.00169, 0.00019, 0.0028),
    2028: (0.00063, 0.00113, 0.00013, 0.0019),
    2029: (0.00031, 0.00056, 0.00006, 0.0009),
}
TA_CVAE_RATE = {2024: 0.0923, 2025: 0.0923, 2026: 0.0923, 2027: 0.0923, 2028: 0.1384, 2029: 0.2768}
CVAE_COLLECTION_THRESHOLD = 63
CVAE_DECLARATION_TURNOVER = 152_500
CVAE_INSTALMENT_THRESHOLD = 1_500
# Dégrèvement des entreprises au CA < 2 M€ (art. 1586 quater), proportionnel au taux
# maximal : 188 € au taux de 0,28 % (2024 à 2027), 125 € à 0,19 % (2028), 63 € à 0,09 % (2029).
CVAE_SMALL_RELIEF = {2024: 188, 2025: 188, 2026: 188, 2027: 188, 2028: 125, 2029: 63}


def _r(x):
    return round(float(x or 0), 2)


def _months(start, end):
    return max(1.0, ((end - start).days + 1) / 365.0 * 12)


# ------------------------------------------------------------------------------- IS
def pme_eligible(turnover, start, end, capital_libere, detention_personnes_physiques):
    """Conditions du taux réduit : CA HT (ramené à 12 mois) ≤ 10 M€, capital libéré,
    détention à 75 % au moins par des personnes physiques."""
    ca12 = float(turnover or 0) * 12 / _months(start, end)
    return bool(ca12 <= IS_PME_TURNOVER_MAX and capital_libere and detention_personnes_physiques)


def compute_is(resultat_fiscal, start, end, turnover, eligible_pme, credits=0.0, acomptes=0.0):
    base = max(0.0, float(resultat_fiscal or 0))
    plafond = IS_REDUCED_CEILING * _months(start, end) / 12 if eligible_pme else 0.0
    reduced_base = min(base, plafond)
    normal_base = base - reduced_base
    is_reduced = reduced_base * IS_REDUCED_RATE
    is_normal = normal_base * IS_NORMAL_RATE
    is_total = round(is_reduced + is_normal)  # l'IS se liquide à l'euro
    ca12 = float(turnover or 0) * 12 / _months(start, end)
    cs_exempt = bool(eligible_pme and ca12 < SOCIAL_CONTRIBUTION_PME_TURNOVER)
    cs = 0 if cs_exempt else round(max(0.0, is_total - SOCIAL_CONTRIBUTION_ALLOWANCE) * SOCIAL_CONTRIBUTION_RATE)
    credits = _r(credits)
    acomptes = _r(acomptes)
    due = _r(is_total + cs - credits)
    solde = _r(due - acomptes)
    return {
        'base': _r(base), 'plafond_taux_reduit': _r(plafond), 'base_taux_reduit': _r(reduced_base),
        'base_taux_normal': _r(normal_base), 'is_taux_reduit': _r(is_reduced), 'is_taux_normal': _r(is_normal),
        'is_total': float(is_total), 'contribution_sociale': float(cs), 'cs_exempt': cs_exempt,
        'credits': credits, 'is_du': due, 'acomptes_verses': acomptes, 'solde': solde,
        'eligible_pme': bool(eligible_pme),
    }


def instalment_dates(start, end):
    """Acomptes d'IS : 15 mars, 15 juin, 15 septembre, 15 décembre compris dans l'exercice."""
    out = []
    for year in range(start.year, end.year + 1):
        for month in (3, 6, 9, 12):
            d = date(year, month, 15)
            if start <= d <= end:
                out.append(d)
    return out


def _add_months(d, months):
    y, m = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m + 1, min(d.day, 28) if d.day > 28 else d.day)


def is_balance_deadline(end):
    """Relevé de solde 2572 : le 15 du 4e mois suivant la clôture (15 mai pour une clôture
    au 31 décembre)."""
    if end.month == 12 and end.day == 31:
        return date(end.year + 1, 5, 15)          # exception légale des clôtures au 31/12
    m = _add_months(date(end.year, end.month, 1), 4)
    return date(m.year, m.month, 15)


def second_working_day_after_may_1(year):
    d = date(year, 5, 1)
    count = 0
    holidays = {date(year, 5, 1), date(year, 5, 8)}
    while count < 2:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in holidays:
            count += 1
    return d


def liasse_deadline(end):
    """Dépôt de la liasse (régime normal) : 2e jour ouvré suivant le 1er mai pour une
    clôture au 31 décembre, sinon dans les 3 mois de la clôture ; + 15 jours en
    télédéclaration."""
    if end.month == 12 and end.day == 31:
        legal = second_working_day_after_may_1(end.year + 1)
    else:
        m = _add_months(date(end.year, end.month, 1), 4)
        legal = date(m.year, m.month, 1) - timedelta(days=1)
    return legal, legal + timedelta(days=15)


def next_instalments(is_reference, next_start, next_end):
    """Acomptes de l'exercice suivant : un quart de l'IS de référence chacun, si celui-ci
    dépasse 3 000 €."""
    if float(is_reference or 0) <= IS_INSTALMENT_THRESHOLD:
        return []
    amount = round(float(is_reference) / 4)
    return [{'date': d, 'amount': float(amount)} for d in instalment_dates(next_start, next_end)]


# ----------------------------------------------------------------------------- CVAE
def cvae_rate(turnover12, year):
    if year >= 2030:
        return 0.0
    # Avant 2026 : même barème qu'en 2026 (taux gelés au niveau 2024).
    sched = CVAE_SCHEDULE.get(year) or CVAE_SCHEDULE[2026 if year < 2026 else 2029]
    r1, r2, r3, rmax = sched
    ca = float(turnover12 or 0)
    if ca < 500_000:
        return 0.0
    if ca <= 3_000_000:
        return r1 * (ca - 500_000) / 2_500_000
    if ca <= 10_000_000:
        return r1 + r2 * (ca - 3_000_000) / 7_000_000
    if ca <= 50_000_000:
        return r1 + r2 + r3 * (ca - 10_000_000) / 40_000_000
    return rmax


def compute_cvae(turnover, valeur_ajoutee, start, end, previous_cvae=None):
    """CVAE due au titre de l'année de clôture de l'exercice."""
    year = end.year
    months = _months(start, end)
    ca = float(turnover or 0)
    ca12 = ca * 12 / months
    va = max(0.0, float(valeur_ajoutee or 0))
    cap = 0.8 if ca12 <= 7_600_000 else 0.85
    va_capped = min(va, ca * cap)
    rate = cvae_rate(ca12, year)
    gross = va_capped * rate
    relief = CVAE_SMALL_RELIEF.get(year, 0) if ca12 < 2_000_000 and gross > 0 else 0
    net = max(0.0, gross - relief)
    if net <= CVAE_COLLECTION_THRESHOLD:
        net = 0.0
    net = float(round(net))
    ta = float(round(net * TA_CVAE_RATE.get(year, 0.0)))
    instalments = []
    if net > CVAE_INSTALMENT_THRESHOLD:
        instalments = [{'date': date(year + 1, 6, 15), 'amount': float(round(net / 2))},
                       {'date': date(year + 1, 9, 15), 'amount': float(round(net / 2))}]
    return {
        'year': year, 'turnover': _r(ca), 'turnover_12m': _r(ca12), 'valeur_ajoutee': _r(va),
        'plafond_va_pct': int(cap * 100), 'va_plafonnee': _r(va_capped), 'taux': rate,
        'taux_pct': round(rate * 100, 4), 'cotisation_brute': _r(gross), 'degrevement': float(relief),
        'cvae': net, 'taxe_additionnelle': ta, 'total': _r(net + ta),
        'declaration_obligatoire': ca12 > CVAE_DECLARATION_TURNOVER,
        'echeance_solde': second_working_day_after_may_1(year + 1),
        'acomptes_annee_suivante': instalments,
        'supprimee': year >= 2030,
    }


# ------------------------------------------------------------------- comptabilité
def acomptes_from_ledger(conn, start, end, entity_id=None):
    """Acomptes d'IS versés pendant l'exercice : débits du compte 444 (hors à-nouveaux)."""
    ef = 'e.entity_id=?' if entity_id else 'e.entity_id IS NULL'
    ep = (entity_id,) if entity_id else ()
    row = conn.execute(
        f"""SELECT COALESCE(SUM(l.debit),0) d FROM accounting_entry_lines l JOIN accounting_entries e ON e.id=l.entry_id
            WHERE l.account_code LIKE ? AND e.journal_code<>'AN' AND e.entry_date BETWEEN ? AND ? AND {ef}""",
        ('444%', start.isoformat(), end.isoformat(), *ep)).fetchone()
    return _r(row['d'] if row else 0)


def generate_is_entry(conn, end, amount, entity_id=None, created_by=None):
    """Écriture de l'IS de l'exercice au jour de la clôture : 695000 / 444000. Une seule
    par exercice et par entité."""
    from profitos.accounting import create_entry, AccountingError, _entry_already_exists
    amount = _r(amount)
    if amount <= 0:
        raise AccountingError("Aucun impôt sur les sociétés à comptabiliser pour cet exercice.")
    source_id = int(end.strftime('%Y%m%d'))
    if _entry_already_exists(conn, 'corporate_tax', source_id, entity_id):
        raise AccountingError("L'impôt sur les sociétés de cet exercice est déjà comptabilisé.")
    return create_entry(conn, 'OD', end, f"Impôt sur les sociétés — exercice clos le {end.strftime('%d/%m/%Y')}",
                        [{'account_code': '695000', 'debit': amount, 'label': 'IS de l\'exercice'},
                         {'account_code': '444000', 'credit': amount, 'label': 'IS de l\'exercice'}],
                        source_type='corporate_tax', source_id=source_id, created_by=created_by, entity_id=entity_id)


def fiscal_calendar(start, end, is_result, cvae_result):
    """Échéances fiscales liées à l'exercice, triées par date."""
    items = []
    legal, extended = liasse_deadline(end)
    items.append({'date': extended, 'label': 'Dépôt de la liasse fiscale (2065 et annexes 2050 à 2059), délai télédéclaration',
                  'form': '2065', 'amount': None})
    items.append({'date': is_balance_deadline(end), 'label': "Relevé de solde de l'impôt sur les sociétés",
                  'form': '2572', 'amount': max(0.0, is_result['solde'])})
    try:
        nxt_end = date(end.year + 1, end.month, end.day)
    except ValueError:
        nxt_end = date(end.year + 1, end.month, 28)
    for a in next_instalments(is_result['is_total'], end + timedelta(days=1), nxt_end):
        items.append({'date': a['date'], 'label': "Acompte d'impôt sur les sociétés", 'form': '2571', 'amount': a['amount']})
    if cvae_result and cvae_result['declaration_obligatoire']:
        items.append({'date': cvae_result['echeance_solde'], 'label': 'Déclaration de valeur ajoutée et solde de CVAE',
                      'form': '1330-CVAE / 1329-DEF', 'amount': cvae_result['total'] or None})
        for a in cvae_result['acomptes_annee_suivante']:
            items.append({'date': a['date'], 'label': 'Acompte de CVAE', 'form': '1329-AC', 'amount': a['amount']})
    return sorted(items, key=lambda x: x['date'])


# --------------------------------------------------------------------- paramètres
DEFAULT_SETTINGS = {'capital_libere': True, 'detention_pp': True, 'deficits_anterieurs': 0.0,
                    'credits_impot': 0.0, 'acomptes_verses': None, 'reintegrations': [], 'deductions': [],
                    'mvlt_anterieures': 0.0, 'tva_acomptes_verses': None}


def _ekey(entity_id):
    return int(entity_id) if entity_id else 0


def get_fiscal_settings(conn, entity_id, fy_end):
    import json
    row = conn.execute('SELECT data FROM fiscal_settings WHERE entity_key=? AND fy_end=?',
                       (_ekey(entity_id), str(fy_end)[:10])).fetchone()
    data = dict(DEFAULT_SETTINGS)
    if row and row['data']:
        try:
            data.update(json.loads(row['data']))
        except ValueError:
            pass
    return data


def save_fiscal_settings(conn, entity_id, fy_end, data, now_iso, user=None):
    import json
    clean = dict(DEFAULT_SETTINGS)
    clean.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    conn.execute(
        """INSERT INTO fiscal_settings(entity_key,fy_end,data,updated_at,updated_by) VALUES(?,?,?,?,?)
           ON CONFLICT(entity_key,fy_end) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at,
           updated_by=excluded.updated_by""",
        (_ekey(entity_id), str(fy_end)[:10], json.dumps(clean, ensure_ascii=False), now_iso, user))
    conn.commit()
    return clean


# ------------------------------------------------------------ profil fiscal (entreprise)
PROFILE_KEY = 'profil'
DEFAULT_PROFILE = {
    'tax_regime': 'normal', 'vat_filing': 'ca3_mensuelle', 'activite': '', 'pme_innovante': False,
    'groupe': False, 'societe_mere_siren': '', 'expert_comptable': '', 'expert_comptable_tel': '', 'conseil': '',
    'signataire': '', 'signataire_qualite': '', 'mel': '', 'shareholders': [], 'subsidiaries': [],
    'mandate': {'iban': '', 'bic': '', 'holder': '', 'rum': '', 'signed_at': '', 'bank_confirmed': False},
    'date_creation': '', 'premises': [],
}
VAT_FILINGS = {'ca3_mensuelle': 'CA3 mensuelle (régime réel normal)', 'ca3_trimestrielle': 'CA3 trimestrielle (TVA due < 4 000 € / an)',
               'ca12': 'CA12 annuelle (régime simplifié)', 'franchise': 'Franchise en base (pas de déclaration de TVA)'}


def get_fiscal_profile(conn, entity_id):
    import json
    row = conn.execute('SELECT data FROM fiscal_settings WHERE entity_key=? AND fy_end=?',
                       (_ekey(entity_id), PROFILE_KEY)).fetchone()
    data = json.loads(json.dumps(DEFAULT_PROFILE))
    if row and row['data']:
        try:
            stored = json.loads(row['data'])
            data.update({k: v for k, v in stored.items() if k in DEFAULT_PROFILE})
            data['mandate'] = dict(DEFAULT_PROFILE['mandate'], **(stored.get('mandate') or {}))
        except ValueError:
            pass
    return data


def save_fiscal_profile(conn, entity_id, data, now_iso, user=None):
    import json
    clean = get_fiscal_profile(conn, entity_id)
    clean.update({k: v for k, v in data.items() if k in DEFAULT_PROFILE})
    conn.execute(
        """INSERT INTO fiscal_settings(entity_key,fy_end,data,updated_at,updated_by) VALUES(?,?,?,?,?)
           ON CONFLICT(entity_key,fy_end) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at,
           updated_by=excluded.updated_by""",
        (_ekey(entity_id), PROFILE_KEY, json.dumps(clean, ensure_ascii=False), now_iso, user))
    conn.commit()
    return clean


def mandate_ready(profile):
    m = (profile or {}).get('mandate') or {}
    from profitos.fr_identifiers import iban_is_valid
    return bool(m.get('iban') and iban_is_valid(m['iban']) and m.get('rum') and m.get('signed_at') and m.get('bank_confirmed'))


def masked_iban(iban):
    s = ''.join(str(iban or '').split()).upper()
    return f"{s[:4]} •••• •••• {s[-4:]}" if len(s) > 8 else s

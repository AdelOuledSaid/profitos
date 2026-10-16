"""Impôts locaux de l'entreprise :

- CFE (cotisation foncière des entreprises) : montant de l'avis d'imposition saisi par
  l'entreprise, acompte de 50 % au 15 juin si la CFE de l'année précédente atteint
  3 000 € (sauf mensualisation), solde au 15 décembre ; exonération l'année de la
  création, base réduite de moitié l'année suivante ; déclaration 1447-C l'année de la
  création, 1447-M en cas de changement (au plus tard le deuxième jour ouvré suivant le
  1er mai) ;
- DECLOYER : loyers des locaux professionnels occupés au 1er janvier, déposés chaque
  année avec la liasse (EDI-TDFC), à partir des locaux du profil fiscal.
"""
from datetime import date

CFE_INSTALMENT_THRESHOLD = 3000.0
CFE_ACCOUNT = '635110'
CFE_PAYABLE_ACCOUNT = '447000'
RENT_ACCOUNT_PREFIX = '6132'


def _r(x):
    return round(float(x or 0), 2)


def _store_key(year):
    return f'cfe-{int(year)}'


def get_cfe(conn, entity_id, year):
    from profitos.tiers_declarations import load_store
    data = {'montant': None, 'montant_n1': None, 'mensualise': False, 'changements': False, 'entry_id': None}
    data.update(load_store(conn, entity_id, _store_key(year)))
    return data


def save_cfe(conn, entity_id, year, data, now_iso, user=None):
    from profitos.tiers_declarations import save_store
    current = get_cfe(conn, entity_id, year)
    current.update({k: v for k, v in data.items() if k in current})
    save_store(conn, entity_id, _store_key(year), current, now_iso, user)
    return current


def cfe_schedule(year, data, creation_date=None):
    """Échéancier et situation de la CFE de l'année."""
    from profitos.corporate_tax import second_working_day_after_may_1
    created = None
    if creation_date:
        try:
            created = date.fromisoformat(str(creation_date)[:10])
        except ValueError:
            created = None
    notes, items = [], []
    exempt = bool(created and created.year == year)
    if exempt:
        notes.append("Année de création : pas de CFE. Déposez la déclaration initiale 1447-C avant le 31 décembre.")
        items.append({'date': date(year, 12, 31), 'label': 'Déclaration initiale de CFE (1447-C)', 'form': '1447-C', 'amount': None})
    elif created and created.year == year - 1:
        notes.append("Première année d'imposition : la base de la CFE est réduite de moitié.")
    montant = _r(data.get('montant')) if data.get('montant') not in (None, '') else None
    n1 = _r(data.get('montant_n1')) if data.get('montant_n1') not in (None, '') else None
    acompte = 0.0
    if not exempt and not data.get('mensualise') and n1 is not None and n1 >= CFE_INSTALMENT_THRESHOLD:
        acompte = _r(n1 / 2)
        items.append({'date': date(year, 6, 15), 'label': 'Acompte de CFE (50 % de la CFE de l\'année précédente)',
                      'form': 'CFE', 'amount': acompte})
    if not exempt:
        if data.get('mensualise'):
            notes.append('Paiement mensualisé : prélèvements de janvier à octobre, régularisation en fin d\'année.')
        solde = _r(montant - acompte) if montant is not None else None
        items.append({'date': date(year, 12, 15), 'label': 'Solde de la CFE' if acompte else 'CFE de l\'année',
                      'form': 'CFE', 'amount': solde})
    if data.get('changements'):
        # Changements intervenus pendant l'année (surface, activité, établissement) : déclarés
        # au plus tard le deuxième jour ouvré suivant le 1er mai de l'année suivante.
        items.append({'date': second_working_day_after_may_1(year + 1), 'label': f'Déclaration des changements {year} (1447-M)',
                      'form': '1447-M', 'amount': None})
    if montant is None and not exempt:
        notes.append("Saisissez le montant de l'avis de CFE (disponible en novembre dans votre espace professionnel).")
    return {'year': year, 'montant': montant, 'montant_n1': n1, 'acompte': acompte, 'exempt': exempt,
            'schedule': sorted(items, key=lambda x: x['date']), 'notes': notes}


def generate_cfe_entry(conn, year, amount, entry_date, entity_id=None, created_by=None):
    """Charge de CFE de l'année : débit 635110, crédit 447000. Une seule par année."""
    from profitos.accounting import create_entry, AccountingError
    amount = _r(amount)
    if amount <= 0:
        raise AccountingError("Montant de CFE à comptabiliser invalide.")
    ef = 'entity_id=?' if entity_id else 'entity_id IS NULL'
    ep = (entity_id,) if entity_id else ()
    if conn.execute(f"SELECT 1 FROM accounting_entries WHERE source_type='cfe' AND source_id=? AND {ef}",
                    (int(year), *ep)).fetchone():
        raise AccountingError(f"La CFE {year} est déjà comptabilisée.")
    return create_entry(conn, 'OD', entry_date, f'Cotisation foncière des entreprises {year}',
                        [{'account_code': CFE_ACCOUNT, 'debit': amount},
                         {'account_code': CFE_PAYABLE_ACCOUNT, 'credit': amount}],
                        source_type='cfe', source_id=int(year), entity_id=entity_id, created_by=created_by)


# --------------------------------------------------------------------- DECLOYER
def rent_booked(conn, start, end, entity_id=None):
    ef = 'e.entity_id=?' if entity_id else 'e.entity_id IS NULL'
    ep = (entity_id,) if entity_id else ()
    return _r(conn.execute(
        f"""SELECT COALESCE(SUM(l.debit-l.credit),0) t FROM accounting_entry_lines l JOIN accounting_entries e ON e.id=l.entry_id
            WHERE {ef} AND e.journal_code<>'AN' AND e.entry_date BETWEEN ? AND ? AND l.account_code LIKE ?""",
        (*ep, str(start)[:10], str(end)[:10], RENT_ACCOUNT_PREFIX + '%')).fetchone()['t'])


def decloyer(profile, rent_in_ledger=0.0):
    """Locaux à déclarer et contrôles."""
    premises = [p for p in ((profile or {}).get('premises') or []) if p.get('address') or p.get('invariant')]
    warnings = []
    if not premises and rent_in_ledger > 0:
        warnings.append("Des loyers immobiliers sont comptabilisés (compte 6132) : renseignez vos locaux professionnels "
                        "dans le profil fiscal pour la déclaration des loyers (DECLOYER).")
    for p in premises:
        if not p.get('invariant'):
            warnings.append(f"Local {p.get('address') or ''} : identifiant du local manquant (DECLOYER).")
    total = _r(sum(float(p.get('rent') or 0) for p in premises))
    if premises and rent_in_ledger and abs(total - rent_in_ledger) > max(1.0, 0.2 * rent_in_ledger):
        warnings.append(f"Loyers déclarés ({total:,.0f} €) très différents des loyers comptabilisés ({rent_in_ledger:,.0f} €) : "
                        "vérifiez la DECLOYER (loyer annuel hors taxes et hors charges).".replace(',', ' '))
    return {'premises': premises, 'total': total, 'warnings': warnings}


def decloyer_rows(d):
    return [{'Formulaire': 'DECLOYER', 'Case': p.get('invariant') or '', 'Libellé': f"Loyer annuel — {p.get('address') or ''}",
             'Montant': int(round(float(p.get('rent') or 0)))} for p in d['premises']]

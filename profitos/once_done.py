"""Once & Done (v482) : un dossier entreprise unique et vérifié, réutilisé par les démarches.

Chaque information n'est saisie qu'une fois, là où ProfitOS la range déjà (profil
entreprise, profil fiscal, veille de secteur), puis réutilisée par tous les documents.
Le dossier vérifie chaque donnée (clés SIRET / TVA / IBAN, formats) et leur cohérence
entre les écrans ; un document n'est préparé que si toutes les données qu'il utilise
sont vérifiées (moteur de vérification commun).

Démarches préparées : fiche de référencement fournisseur (pour vos clients), mise en
demeure de payer, lettre de résiliation d'un contrat fournisseur, demande de délai de
paiement au service des impôts des entreprises.
"""
import re
from datetime import date, timedelta

from profitos import verification as V
from profitos.fr_identifiers import fr_vat_is_valid, iban_is_valid, siren_is_valid, siret_is_valid

MODULE = 'once_done'
STORE_KEY = 'once-done'
ONCE_FIELDS = ('telephone',)          # seules informations sans autre place dans ProfitOS


def _store(c, eid):
    from profitos.tiers_declarations import load_store
    return load_store(c, eid, STORE_KEY) or {}


def _fr(value):
    try:
        d = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
        return d.strftime('%d/%m/%Y')
    except (TypeError, ValueError):
        return ''


# --------------------------------------------------------------------- dossier
def dossier(c, eid, owner_email=None):
    """Toutes les informations de l'entreprise avec leur source et leur contrôle :
    {key: {'label', 'value', 'source', 'check': check(), 'edit': endpoint}}."""
    from profitos import corporate_tax as CT
    from profitos import market_watch as MW
    from profitos.routes.invoicing import _seller_identity
    ident = _seller_identity(c, eid)
    fiscal = CT.get_fiscal_profile(c, eid)
    watch = MW.load_settings(c, eid)
    extra = _store(c, eid)
    franchise = (ident.get('vat_regime') or '') == 'franchise_293B'
    siret = re.sub(r'\D', '', ident.get('siret') or '')
    siren = siret[:9]
    out = {}

    def add(key, label, value, source, ok, detail='', edit='company'):
        out[key] = {'label': label, 'value': value, 'source': source, 'edit': edit,
                    'check': V.check(label, ok, detail, source)}

    P, F, W, O = 'Profil entreprise', 'Profil fiscal', 'Veille de votre secteur', 'Dossier entreprise'
    name = (ident.get('name') or '').strip()
    add('name', 'Dénomination', name, P, bool(name) or None, '' if name else 'À renseigner.')
    lf = ident.get('legal_form') or ''
    add('legal_form', 'Forme juridique', lf, P, bool(lf) or None, '' if lf else 'À renseigner.')
    siret_ok = siret_is_valid(siret) if siret else None
    add('siret', 'SIRET', siret, P, siret_ok, 'À renseigner.' if not siret else ('' if siret_ok else 'Clé de contrôle incorrecte.'))
    add('siren', 'SIREN', siren, 'Déduit du SIRET', (siren_is_valid(siren) if siren else None), '' if siren else 'Déduit du SIRET.')
    vat = (ident.get('vat_number') or '').replace(' ', '').upper()
    if franchise:
        add('vat_number', 'N° de TVA intracommunautaire', vat or 'Franchise en base (art. 293 B du CGI)', P, True)
    else:
        ok = (fr_vat_is_valid(vat, siren or None) if vat else None)
        add('vat_number', 'N° de TVA intracommunautaire', vat, P, ok,
            'À renseigner.' if not vat else ('' if ok else 'Ne correspond pas au SIREN ou clé incorrecte.'))
    addr = ' '.join(x for x in ((ident.get('address') or '').replace('\n', ', '), ident.get('postal_code') or '',
                                ident.get('city') or '') if x).strip()
    add('address', 'Adresse du siège', addr, P,
        True if (ident.get('address') and ident.get('postal_code') and ident.get('city')) else None,
        '' if ident.get('address') and ident.get('postal_code') and ident.get('city') else 'Adresse, code postal et ville à renseigner.')
    from profitos.company_profile import LEGAL_FORMS_WITH_CAPITAL, LEGAL_FORMS_WITHOUT_REGISTRY
    if lf in LEGAL_FORMS_WITH_CAPITAL:
        cap = ident.get('share_capital') or ''
        add('share_capital', 'Capital social', f'{cap} €' if cap else '', P, bool(cap) or None, '' if cap else 'À renseigner.')
    if lf not in LEGAL_FORMS_WITHOUT_REGISTRY:
        reg = (f"RCS {ident['rcs_city']}" if ident.get('rcs_city') else '') or (f"RM {ident['rm_number']}" if ident.get('rm_number') else '')
        add('registry', 'Immatriculation', reg, P, bool(reg) or None, '' if reg else 'Ville du RCS ou n° RM à renseigner.')
    naf = (watch.get('naf') or '').upper().replace('.', '')
    naf_ok = bool(re.fullmatch(r'\d{4}[A-Z]', naf)) if naf else None
    add('naf', 'Code APE', naf, W, naf_ok, 'À renseigner (ex. 4321A).' if not naf else ('' if naf_ok else 'Format attendu : 4 chiffres et une lettre.'),
        edit='once_done')
    iban = (ident.get('iban') or '').replace(' ', '').upper()
    add('iban', 'IBAN', iban, P, (iban_is_valid(iban) if iban else None),
        'À renseigner.' if not iban else ('' if iban_is_valid(iban) else 'Clé de contrôle incorrecte.'))
    bic = (ident.get('bic') or '').upper()
    add('bic', 'BIC', bic, P, (bool(re.fullmatch(r'[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?', bic)) if bic else None),
        'À renseigner.' if not bic else '')
    rep = (fiscal.get('signataire') or '').strip()
    add('representant', 'Représentant légal', rep, F, bool(rep) or None, '' if rep else 'À renseigner une fois.', 'once_done')
    qual = (fiscal.get('signataire_qualite') or '').strip()
    add('qualite', 'Qualité du représentant', qual, F, bool(qual) or None, '' if qual else 'À renseigner une fois (ex. Président).',
        'once_done')
    mel = (fiscal.get('mel') or owner_email or '').strip()
    add('email', 'E-mail de contact', mel, F if fiscal.get('mel') else 'Compte du propriétaire',
        (bool(re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', mel)) if mel else None), '' if mel else 'À renseigner une fois.', 'once_done')
    tel = (extra.get('telephone') or '').strip()
    add('telephone', 'Téléphone', tel, O, (bool(re.fullmatch(r'\+?[0-9 .\-()]{8,20}', tel)) if tel else None),
        '' if tel else 'À renseigner une fois.', 'once_done')
    created = fiscal.get('date_creation') or ''
    add('date_creation', 'Date de création', _fr(created) or created, F, (bool(_fr(created)) if created else None),
        '' if created else 'À renseigner une fois.', 'once_done')

    # Cohérence entre les écrans : une contradiction est bloquante.
    filing = fiscal.get('vat_filing') or ''
    if franchise and filing and filing != 'franchise':
        out['vat_regime'] = {'label': 'Régime de TVA', 'value': 'franchise (profil entreprise) / ' + filing + ' (profil fiscal)',
                             'source': 'Profil entreprise et profil fiscal', 'edit': 'company',
                             'check': V.check('Régime de TVA cohérent', False,
                                              'Franchise en base sur les factures mais déclarations de TVA prévues au profil fiscal.',
                                              'Profil entreprise et profil fiscal')}
    elif not franchise and filing == 'franchise':
        out['vat_regime'] = {'label': 'Régime de TVA', 'value': 'TVA facturée / franchise au profil fiscal',
                             'source': 'Profil entreprise et profil fiscal', 'edit': 'company',
                             'check': V.check('Régime de TVA cohérent', False,
                                              'TVA facturée mais franchise en base indiquée au profil fiscal.',
                                              'Profil entreprise et profil fiscal')}
    if naf_ok and watch.get('sector') and MW.sector_from_naf(naf) and MW.sector_from_naf(naf) != watch['sector']:
        out['naf']['check'] = V.check('Code APE', None,
                                      f"Le code APE correspond au secteur « {MW.SECTORS[MW.sector_from_naf(naf)]['label']} », "
                                      f"la veille suit « {MW.SECTORS[watch['sector']]['label']} » : à confirmer.", W)
    return out


def save_once(c, eid, form, now_iso, user):
    """Enregistre les informations saisies une fois, chacune à sa place d'origine."""
    from profitos import corporate_tax as CT
    from profitos import market_watch as MW
    from profitos.tiers_declarations import save_store
    errors = []
    fiscal_update = {}
    for key, field in (('representant', 'signataire'), ('qualite', 'signataire_qualite'), ('email', 'mel')):
        if key in form:
            fiscal_update[field] = ' '.join(str(form.get(key) or '').split())[:120]
    if fiscal_update.get('mel') and not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', fiscal_update['mel']):
        errors.append('E-mail de contact invalide.')
    if 'date_creation' in form:
        d = (form.get('date_creation') or '').strip()
        if d and not _fr(d):
            errors.append('Date de création invalide.')
        fiscal_update['date_creation'] = d
    naf = None
    if 'naf' in form:
        naf = re.sub(r'[^0-9A-Za-z]', '', form.get('naf') or '').upper()[:5]
        if naf and not re.fullmatch(r'\d{4}[A-Z]', naf):
            errors.append('Code APE invalide : 4 chiffres et une lettre (ex. 4321A).')
    tel = ' '.join(str(form.get('telephone') or '').split())[:20] if 'telephone' in form else None
    if tel and not re.fullmatch(r'\+?[0-9 .\-()]{8,20}', tel):
        errors.append('Téléphone invalide.')
    if errors:
        return errors
    if fiscal_update:
        CT.save_fiscal_profile(c, eid, fiscal_update, now_iso, user)
    if naf is not None:
        settings = MW.load_settings(c, eid)
        settings['naf'] = naf
        MW.save_settings(c, eid, settings, now_iso, user)
    if tel is not None:
        extra = _store(c, eid)
        extra['telephone'] = tel
        save_store(c, eid, STORE_KEY, extra, now_iso, user)
    return []


# ------------------------------------------------------------------- démarches
DOCUMENTS = {
    'fiche_fournisseur': {'title': 'Fiche de référencement fournisseur', 'area': 'settings',
                          'why': "Demandée par vos clients pour vous créer comme fournisseur : identité, immatriculation, "
                                 "TVA, coordonnées bancaires et contact.",
                          'needs': ('name', 'legal_form', 'siret', 'siren', 'vat_number', 'address', 'naf', 'iban', 'bic',
                                    'representant', 'qualite', 'email')},
    'mise_en_demeure': {'title': 'Mise en demeure de payer', 'area': 'invoicing',
                        'why': "Dernière étape amiable avant une procédure : rappelle la facture, les pénalités de retard et "
                               "l'indemnité forfaitaire de 40 € pour frais de recouvrement (art. L441-10 et D441-5 du Code de commerce).",
                        'needs': ('name', 'siret', 'address', 'representant', 'qualite')},
    'resiliation': {'title': "Résiliation d'un contrat fournisseur", 'area': 'invoicing',
                    'why': "Lettre à envoyer en recommandé avec accusé de réception avant la date limite de préavis "
                           "(voir Contrats fournisseurs).",
                    'needs': ('name', 'siret', 'address', 'representant', 'qualite')},
    'delai_sie': {'title': 'Demande de délai de paiement (impôts)', 'area': 'accounting',
                  'why': "Demande de plan de règlement au service des impôts des entreprises, à déposer par la messagerie "
                         "sécurisée de votre espace professionnel sur impots.gouv.fr.",
                  'needs': ('name', 'siren', 'address', 'representant', 'qualite', 'email')},
}


def readiness(doc_key, info):
    """Contrôles d'un document : une ligne par donnée utilisée, plus les contradictions."""
    doc = DOCUMENTS[doc_key]
    checks = [info[k]['check'] for k in doc['needs'] if k in info]
    if 'vat_regime' in info:
        checks.append(info['vat_regime']['check'])
    return checks, V.level(checks)


def overdue_invoices(c, eid, today):
    rows = c.execute("""SELECT * FROM outgoing_invoices WHERE entity_id IS ? AND status IN ('sent','partially_paid','overdue')
                        AND due_date IS NOT NULL AND due_date<? ORDER BY due_date, id""", (eid, today.isoformat())).fetchall()
    from profitos.routes.invoicing import _invoice_balance
    out = []
    for r in rows:
        bal = _invoice_balance(c, r)
        if bal > 0.005:
            out.append(dict(r, balance=round(bal, 2)))
    return out


def letter_context(c, eid, doc_key, info, form, today):
    """Données du document (après contrôle). Lève ValueError avec un message en français."""
    ctx = {'today': today, 'info': {k: v['value'] for k, v in info.items()}, 'doc': DOCUMENTS[doc_key], 'doc_key': doc_key}
    if doc_key == 'mise_en_demeure':
        inv_id = int(form.get('invoice_id') or 0)
        inv = next((x for x in overdue_invoices(c, eid, today) if x['id'] == inv_id), None)
        if not inv:
            raise ValueError('Choisissez une facture échue et non réglée.')
        if not (inv.get('client_address') or '').strip():
            raise ValueError("L'adresse du client manque sur la facture : complétez-la avant d'envoyer une mise en demeure.")
        from profitos.routes.invoicing import _seller_identity
        ident = _seller_identity(c, eid)
        days = int(form.get('delay_days') or 8)
        if not 8 <= days <= 30:
            raise ValueError('Délai de paiement accordé : entre 8 et 30 jours.')
        ctx.update(invoice=inv, delay_days=days, deadline=today + timedelta(days=days),
                   penalty=ident.get('late_penalty_rate') or "trois fois le taux d'intérêt légal")
    elif doc_key == 'resiliation':
        from profitos import contract_guardian as CG
        k = CG.get(c, eid, int(form.get('contract_id') or 0))
        if not k:
            raise ValueError('Choisissez un contrat fournisseur.')
        addr = ' '.join(str(form.get('supplier_address') or '').split())[:300]
        if not addr:
            raise ValueError("Indiquez l'adresse du fournisseur.")
        end, deadline = CG.current_term(k, today)
        ctx.update(contract=k, supplier_address=addr, term_end=end, notice_deadline=deadline,
                   late=bool(deadline and deadline < today))
        # Adresse saisie une fois : rangée dans la fiche du fournisseur s'il est connu.
        if k.get('supplier_id'):
            c.execute("UPDATE suppliers SET address=? WHERE id=? AND COALESCE(address,'')=''", (addr, k['supplier_id']))
            c.commit()
    elif doc_key == 'delai_sie':
        from profitos.runtime import parse_amount
        tax = ' '.join(str(form.get('tax') or '').split())[:80]
        amount = parse_amount(form.get('amount') or '')
        months = int(form.get('months') or 0) if str(form.get('months') or '').isdigit() else 0
        reason = ' '.join(str(form.get('reason') or '').split())[:600]
        due = (form.get('due_date') or '').strip()
        if not tax or not amount or amount <= 0:
            raise ValueError("Indiquez l'impôt concerné et le montant dû.")
        if not 2 <= months <= 24:
            raise ValueError('Nombre de mensualités : entre 2 et 24.')
        if not reason:
            raise ValueError('Expliquez brièvement la difficulté de trésorerie (motif de la demande).')
        ctx.update(tax=tax, amount=round(amount, 2), months=months, monthly=round(amount / months, 2), reason=reason,
                   due_date=_fr(due))
    return ctx


def detect(c, eid, today, owner_email=None):
    """Constat Mission Control : données du dossier invalides ou contradictoires (bloquantes)."""
    info = dossier(c, eid, owner_email)
    bad = [v['check'] for v in info.values() if v['check']['ok'] is False]
    if not bad:
        return []
    return [V.finding('dossier:anomalies', MODULE, 'dossier', 'important',
                      f'Dossier entreprise : {len(bad)} donnée(s) invalide(s) ou contradictoire(s)',
                      checks=bad, detail="Ces données sont reprises sur vos factures et vos démarches : corrigez-les une fois.",
                      action={'kind': 'link', 'label': 'Corriger le dossier', 'endpoint': 'once_done'}, follow_up_days=7)]

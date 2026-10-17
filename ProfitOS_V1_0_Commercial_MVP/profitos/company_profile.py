"""Profil entreprise : lecture du formulaire, contrôles et enregistrement.

Le profil alimente les mentions obligatoires des factures (CGI art. 242 nonies A,
C. com. art. L441-9 et R123-237) : forme juridique, capital social, immatriculation
RCS/RM, régime de TVA, conditions de paiement, pénalités de retard et escompte.
"""
import re

from profitos.fr_identifiers import (siret_is_valid, fr_vat_is_valid, iban_is_valid,
                                     fr_postal_code_is_valid)

# Formes juridiques : celles d'une société doivent mentionner le capital social
# (C. com. art. R123-237).
LEGAL_FORMS = ('SAS', 'SASU', 'SARL', 'EURL', 'SA', 'SNC', 'SCI', 'SELARL', 'SELAS', 'SCOP',
               'EI', 'EI (profession libérale)', 'Micro-entreprise', 'Association', 'Autre')
LEGAL_FORMS_WITH_CAPITAL = ('SAS', 'SASU', 'SARL', 'EURL', 'SA', 'SNC', 'SCI', 'SELARL', 'SELAS', 'SCOP')
LEGAL_FORMS_WITHOUT_REGISTRY = ('EI (profession libérale)', 'Association')
VAT_REGIMES = ('normal', 'franchise_293B')
DEFAULT_LATE_PENALTY = "trois fois le taux d'intérêt légal"
DEFAULT_ESCOMPTE = "Pas d'escompte pour paiement anticipé."

TEXT_FIELDS = ('name', 'city', 'department', 'allowed_departments', 'activities', 'certifications',
               'address', 'postal_code', 'siret', 'vat_number', 'iban', 'bic', 'legal_form',
               'share_capital', 'rcs_city', 'rm_number', 'vat_regime', 'late_penalty_rate',
               'escompte_text', 'payment_terms', 'country')
PROFILE_FIELDS = TEXT_FIELDS + ('vat_on_debits',)


def _clean(value, limit=500):
    return (value or '').strip()[:limit]


def parse_company_form(form):
    """Retourne (valeurs normalisées, erreurs en français). Les valeurs sont toujours
    renvoyées, même en cas d'erreur, pour réafficher le formulaire tel que saisi."""
    v = {f: _clean(form.get(f), 2000 if f in ('activities', 'address') else 500) for f in TEXT_FIELDS}
    v['country'] = (v['country'] or 'FR').upper()[:2]
    v['siret'] = re.sub(r'\D', '', v['siret'])
    v['vat_number'] = re.sub(r'[\s.\-]', '', v['vat_number']).upper()
    v['iban'] = re.sub(r'\s', '', v['iban']).upper()
    v['bic'] = re.sub(r'\s', '', v['bic']).upper()
    v['postal_code'] = re.sub(r'\s', '', v['postal_code'])
    # Colonne TEXT (migration tolérante) : '1' / '0'.
    v['vat_on_debits'] = '1' if form.get('vat_on_debits') in ('on', '1', 'true') else '0'
    if v['vat_regime'] not in VAT_REGIMES:
        v['vat_regime'] = 'normal'
    if v['legal_form'] and v['legal_form'] not in LEGAL_FORMS:
        v['legal_form'] = 'Autre'
    if not v['department'] and v['postal_code'][:2].isdigit():
        v['department'] = v['postal_code'][:2]
    if not v['allowed_departments']:
        v['allowed_departments'] = v['department']
    errors = []
    if not v['name']:
        errors.append("La dénomination de l'entreprise est obligatoire.")
    if v['siret'] and not siret_is_valid(v['siret']):
        errors.append("SIRET invalide : il doit comporter 14 chiffres et respecter la clé de contrôle (vérifiez la saisie).")
    if v['vat_number'] and not fr_vat_is_valid(v['vat_number'], v['siret'][:9] or None):
        if v['vat_number'].startswith('FR') and v['siret'] and v['vat_number'][4:] != v['siret'][:9]:
            errors.append("N° de TVA intracommunautaire invalide : il doit reprendre le SIREN (9 premiers chiffres du SIRET).")
        else:
            errors.append("N° de TVA intracommunautaire invalide : vérifiez la clé (2 caractères après « FR ») et le SIREN.")
    if v['iban'] and not iban_is_valid(v['iban']):
        errors.append("IBAN invalide : la clé de contrôle ne correspond pas (vérifiez la saisie).")
    if v['bic'] and not re.fullmatch(r'[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?', v['bic']):
        errors.append("BIC invalide : 8 ou 11 caractères (ex. AGRIFRPP).")
    if v['postal_code'] and not fr_postal_code_is_valid(v['postal_code'], v['country']):
        errors.append("Code postal invalide : 5 chiffres pour une adresse en France.")
    if v['share_capital']:
        raw = v['share_capital'].replace('€', '').replace(' ', '').replace('\xa0', '').replace(' ', '').replace(',', '.')
        try:
            capital = float(raw)
            if capital < 0:
                raise ValueError
            v['share_capital'] = f"{capital:.2f}".rstrip('0').rstrip('.')
        except ValueError:
            errors.append("Capital social invalide : indiquez un montant en euros (ex. 10 000).")
    return v, errors


def save_company_profile(conn, v):
    cols = list(PROFILE_FIELDS) + ['updated_at']
    from profitos.runtime import now
    values = [v.get(f) for f in PROFILE_FIELDS] + [now()]
    placeholders = ','.join('?' for _ in cols)
    updates = ','.join(f'{col}=excluded.{col}' for col in cols)
    conn.execute(f"INSERT INTO company(id,{','.join(cols)}) VALUES(1,{placeholders}) "
                 f"ON CONFLICT(id) DO UPDATE SET {updates}", values)
    conn.commit()


def missing_mandatory_fields(p):
    """Champs indispensables pour émettre une facture conforme, encore vides (libellés français).
    Sert à la case « Profil entreprise complété » du tableau de bord et au démarrage."""
    if not p:
        return ['dénomination', 'forme juridique', 'SIRET', 'adresse', 'code postal', 'ville', 'n° de TVA']
    get = (lambda k: (p.get(k) if hasattr(p, 'get') else p[k]) or '')
    missing = []
    for key, label in (('name', 'dénomination'), ('legal_form', 'forme juridique'), ('siret', 'SIRET'),
                       ('address', 'adresse'), ('postal_code', 'code postal'), ('city', 'ville')):
        if not str(get(key)).strip():
            missing.append(label)
    try:
        regime = get('vat_regime') or 'normal'
    except (KeyError, IndexError):
        regime = 'normal'
    if regime != 'franchise_293B' and not str(get('vat_number')).strip():
        missing.append('n° de TVA')
    if get('legal_form') in LEGAL_FORMS_WITH_CAPITAL and not str(get('share_capital')).strip():
        missing.append('capital social')
    return missing

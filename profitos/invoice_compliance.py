"""Règles de TVA des factures de vente et mentions légales imprimées.

Source unique utilisée par le PDF, la page publique et le XML Factur-X :
- régime de TVA de l'opération -> catégorie EN 16931 (S, E, AE, K, G, Z), mention
  obligatoire et code d'exonération VATEX ;
- mentions de paiement : conditions, pénalités de retard, indemnité forfaitaire de
  40 € (C. com. art. L441-10 et D441-5), escompte ;
- option pour le paiement de la TVA d'après les débits (CGI art. 269, 2-c).
"""
from profitos.company_profile import DEFAULT_LATE_PENALTY, DEFAULT_ESCOMPTE

# Régime de TVA de l'opération -> (catégorie EN 16931, mention obligatoire, code VATEX).
VAT_TREATMENTS = {
    'standard': ('S', None, None),
    'franchise': ('E', "TVA non applicable, art. 293 B du CGI", 'VATEX-FR-FRANCHISE'),
    'reverse_charge_fr': ('AE', "Autoliquidation — art. 283-2 nonies du CGI (TVA due par le preneur)", 'VATEX-EU-AE'),
    'intra_eu_services': ('AE', "Autoliquidation — art. 283-2 du CGI et art. 196 de la directive 2006/112/CE", 'VATEX-EU-AE'),
    'intra_eu_goods': ('K', "Exonération de TVA — livraison intracommunautaire, art. 262 ter I du CGI", 'VATEX-EU-IC'),
    'export': ('G', "Exonération de TVA — exportation, art. 262 I du CGI", 'VATEX-EU-G'),
    'exempt': ('E', None, None),
}
VAT_TREATMENT_LABELS = {
    'standard': 'TVA française (taux normal, intermédiaire ou réduit)',
    'franchise': 'Franchise en base de TVA (art. 293 B du CGI)',
    'reverse_charge_fr': 'Autoliquidation en France (sous-traitance BTP)',
    'intra_eu_services': 'Prestation de services à un client professionnel de l’UE (autoliquidation)',
    'intra_eu_goods': 'Livraison intracommunautaire de biens (exonérée)',
    'export': 'Exportation hors UE (exonérée)',
    'exempt': 'Opération exonérée (préciser l’article)',
}
OPERATION_NATURE_LABELS = {
    'biens': 'Livraison de biens',
    'services': 'Prestation de services',
    'mixte': 'Livraison de biens et prestation de services',
}


def _g(row, key, default=None):
    if row is None:
        return default
    try:
        keys = row.keys()
    except AttributeError:
        return default
    value = row[key] if key in keys else default
    return default if value is None else value


def seller_is_franchise(seller):
    return (_g(seller, 'vat_regime', '') or '') == 'franchise_293B'


def vat_treatment(inv, seller=None):
    """Régime de TVA effectif : la franchise du vendeur prime sur le choix de la facture."""
    if seller is not None and seller_is_franchise(seller):
        return 'franchise'
    t = _g(inv, 'vat_treatment', '') or 'standard'
    return t if t in VAT_TREATMENTS else 'standard'


def vat_category(inv, seller, rate):
    """Catégorie EN 16931 d'une ligne / d'un taux : S si taux > 0 en régime normal, Z pour
    un taux nul en régime normal (BR-Z), sinon la catégorie du régime."""
    t = vat_treatment(inv, seller)
    if t == 'standard':
        return 'S' if float(rate or 0) > 0 else 'Z'
    return VAT_TREATMENTS[t][0]


def vat_exemption(inv, seller):
    """(motif lisible, code VATEX) pour les catégories E/AE/K/G, sinon (None, None)."""
    t = vat_treatment(inv, seller)
    if t == 'standard':
        return None, None
    _, mention, code = VAT_TREATMENTS[t]
    if t == 'exempt':
        return (_g(inv, 'vat_exemption_reason') or 'Opération exonérée de TVA'), None
    return mention, code


def is_b2c(inv):
    return (_g(inv, 'ereporting_scope', '') or '') == 'b2c'


def legal_mentions(inv, seller, fr_date=None, include_payment=True):
    """Mentions légales à imprimer (liste de phrases en français)."""
    fmt = fr_date or (lambda d: d)
    out = []
    t = vat_treatment(inv, seller)
    if t != 'standard':
        reason, _ = vat_exemption(inv, seller)
        if reason:
            out.append(reason)
    elif str(_g(inv, 'vat_on_debits', '0') or '0') in ('1', 'True', 'true') and \
            (_g(inv, 'operation_nature', 'services') or 'services') in ('services', 'mixte'):
        out.append("Option pour le paiement de la TVA d'après les débits.")
    if not include_payment:
        return out
    terms = (_g(seller, 'payment_terms', '') or '').strip()
    due = _g(inv, 'due_date')
    if terms:
        out.append(f"Conditions de paiement : {terms}" + (f" — échéance le {fmt(due)}." if due else '.'))
    elif due:
        out.append(f"Date d'échéance du paiement : {fmt(due)}.")
    if not is_b2c(inv):
        penalty = (_g(seller, 'late_penalty_rate', '') or '').strip() or DEFAULT_LATE_PENALTY
        out.append(f"Pénalités de retard exigibles dès le lendemain de l'échéance : {penalty}.")
        out.append("Indemnité forfaitaire pour frais de recouvrement en cas de retard de paiement : 40 € "
                   "(art. L441-10 et D441-5 du Code de commerce).")
    escompte = (_g(seller, 'escompte_text', '') or '').strip() or DEFAULT_ESCOMPTE
    out.append(escompte if escompte.endswith('.') else escompte + '.')
    iban = (_g(seller, 'iban', '') or '').replace(' ', '')
    if iban:
        grouped = ' '.join(iban[i:i + 4] for i in range(0, len(iban), 4))
        bic = _g(seller, 'bic', '') or ''
        out.append(f"Règlement par virement : IBAN {grouped}" + (f" — BIC {bic}" if bic else '') + '.')
    return out


def seller_legal_footer(seller):
    """Ligne d'identité légale du vendeur (pied de page)."""
    from profitos.runtime import fr_number
    parts = [str(_g(seller, 'name', '') or '')]
    form = _g(seller, 'legal_form', '')
    capital = _g(seller, 'share_capital', '')
    if form and capital:
        try:
            parts.append(f"{form} au capital de {fr_number(float(capital), 2 if float(capital) % 1 else 0)} €")
        except (TypeError, ValueError):
            parts.append(f"{form} au capital de {capital} €")
    elif form:
        parts.append(str(form))
    siret = ''.join(ch for ch in str(_g(seller, 'siret', '') or '') if ch.isdigit())
    rcs = _g(seller, 'rcs_city', '')
    if rcs and len(siret) >= 9:
        parts.append(f"RCS {rcs} {siret[:3]} {siret[3:6]} {siret[6:9]}")
    rm = _g(seller, 'rm_number', '')
    if rm:
        parts.append(str(rm) if str(rm).upper().startswith('RM') else f"RM {rm}")
    if siret:
        parts.append(f"SIRET {siret}")
    if _g(seller, 'vat_number', ''):
        parts.append(f"N° TVA {_g(seller, 'vat_number')}")
    return ' — '.join(p for p in parts if p)

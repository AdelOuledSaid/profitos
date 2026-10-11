"""Comptes annuels / plaquette — génère le bilan, le compte de résultat et
une annexe simplifiée en PDF, à partir des calculs de profitos.accounting
(compute_bilan, compute_compte_resultat).

Suit le même motif fpdf2 déjà établi dans profitos/routes/invoicing.py
(_render_invoice_pdf) : police Helvetica standard + fonction safe() de
nettoyage de texte, pas de police Unicode embarquée — ce document n'a pas
besoin de la conformité Factur-X qui, elle, exige DejaVuSans.
"""
from profitos.runtime import fr_number


def _safe(text):
    if text is None:
        return ''
    text = str(text)
    repl = {'—': '-', '–': '-', '\u2018': "'", '\u2019': "'", '\u201c': '"', '\u201d': '"',
            '…': '...', '\xa0': ' ', '€': 'EUR'}
    for a, b in repl.items():
        text = text.replace(a, b)
    return text.encode('latin-1', errors='replace').decode('latin-1')


def render_plaquette_pdf(company_name, entity_label, as_of_date, exercice_start,
                          actif, passif, resultat, loans_summary, leases_engagement, fixed_assets_summary):
    """Retourne les octets du PDF, ou None si fpdf2 n'est pas installé
    (dégradation propre, jamais d'erreur 500 — même convention que le reste
    de l'app)."""
    try:
        from fpdf import FPDF
    except ImportError:
        return None

    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.set_auto_page_break(auto=True, margin=18)

    def header(title):
        pdf.set_font('Helvetica', 'B', 18); pdf.set_text_color(17, 24, 39)
        pdf.cell(0, 10, _safe(title), ln=1)
        pdf.set_font('Helvetica', '', 10); pdf.set_text_color(107, 114, 128)
        pdf.cell(0, 6, _safe(f"{company_name}{' — ' + entity_label if entity_label else ''}"), ln=1)
        pdf.cell(0, 6, _safe(f"Exercice du {exercice_start} au {as_of_date}"), ln=1)
        pdf.ln(6)

    def line(label, amount, bold=False, indent=0):
        pdf.set_font('Helvetica', 'B' if bold else '', 10)
        pdf.set_text_color(17, 24, 39)
        pdf.cell(6 * indent, 6, '')
        pdf.cell(120 - 6 * indent, 6, _safe(label))
        pdf.cell(40, 6, _safe(f"{fr_number(amount, 2)} EUR"), align='R', ln=1)

    def section_title(title):
        pdf.ln(2)
        pdf.set_font('Helvetica', 'B', 12); pdf.set_text_color(17, 24, 39)
        pdf.cell(0, 8, _safe(title), ln=1)
        pdf.set_draw_color(226, 232, 240); pdf.line(pdf.get_x(), pdf.get_y(), 190, pdf.get_y())
        pdf.ln(3)

    # ---- Page 1 : Bilan ----
    pdf.add_page()
    header('Bilan')
    section_title('ACTIF')
    line('Immobilisations brutes', actif['immobilisations_brutes'])
    line('Amortissements', -actif['amortissements'], indent=1)
    line('Immobilisations nettes', actif['immobilisations_nettes'], bold=True)
    line('Créances (clients, charges constatées d\u2019avance...)', actif['creances'])
    line('Disponibilités (banque, caisse)', actif['disponibilites'])
    pdf.ln(2)
    line('TOTAL ACTIF', actif['total'], bold=True)

    section_title('PASSIF')
    line('Capitaux propres (capital, réserves)', passif['capitaux_propres'])
    line('Résultat de l\u2019exercice', passif['resultat_exercice'])
    line('Emprunts', passif['emprunts'])
    line('Dettes (fournisseurs, fiscales et sociales...)', passif['dettes'])
    pdf.ln(2)
    line('TOTAL PASSIF', passif['total'], bold=True)

    # ---- Page 2 : Compte de résultat ----
    pdf.add_page()
    header('Compte de résultat')
    section_title('PRODUITS')
    for p in resultat['produits']:
        if p['amount']:
            line(f"{p['code']} — {p['label']}", p['amount'])
    line('TOTAL PRODUITS', resultat['total_produits'], bold=True)

    section_title('CHARGES')
    for ch in resultat['charges']:
        if ch['amount']:
            line(f"{ch['code']} — {ch['label']}", ch['amount'])
    line('TOTAL CHARGES', resultat['total_charges'], bold=True)

    pdf.ln(4)
    pdf.set_font('Helvetica', 'B', 13)
    pdf.cell(120, 9, _safe('RÉSULTAT DE L\u2019EXERCICE'))
    pdf.cell(40, 9, _safe(f"{fr_number(resultat['resultat'], 2)} EUR"), align='R', ln=1)

    # ---- Page 3 : Annexe simplifiée ----
    pdf.add_page()
    header('Annexe simplifiée')

    section_title('Immobilisations et amortissements')
    if fixed_assets_summary:
        for a in fixed_assets_summary:
            line(a['label'], a['net_value'])
    else:
        pdf.set_font('Helvetica', 'I', 10); pdf.set_text_color(150, 150, 150)
        pdf.cell(0, 6, _safe('Aucune immobilisation active.'), ln=1)

    section_title('État des emprunts')
    if loans_summary:
        for l in loans_summary:
            line(f"{l['lender_name']} (solde restant dû)", l['remaining_capital'])
    else:
        pdf.set_font('Helvetica', 'I', 10); pdf.set_text_color(150, 150, 150)
        pdf.cell(0, 6, _safe('Aucun emprunt en cours.'), ln=1)

    section_title('Engagements hors bilan — crédit-bail')
    pdf.set_font('Helvetica', '', 10); pdf.set_text_color(17, 24, 39)
    pdf.cell(0, 6, _safe(
        f"Total des redevances de crédit-bail restant à payer sur les contrats en cours : "
        f"{fr_number(leases_engagement, 2)} EUR."
    ), ln=1)

    pdf.ln(10)
    pdf.set_font('Helvetica', 'I', 8); pdf.set_text_color(150, 150, 150)
    pdf.multi_cell(0, 4, _safe(
        "Document généré via ProfitOS à titre de synthèse de gestion. Ne constitue pas une liasse "
        "fiscale ni un document certifié — à faire valider par un expert-comptable avant tout dépôt "
        "ou usage officiel."
    ))

    return bytes(pdf.output(dest='S'))

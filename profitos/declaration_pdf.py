"""Récapitulatif PDF d'une déclaration préparée par ProfitOS : montants case par case,
paiement, mode de dépôt et rappel que ProfitOS ne transmet pas lui-même la déclaration.
Même convention fpdf2 que la plaquette (Helvetica, texte ramené en latin-1)."""
from profitos.plaquette import _safe as _plaquette_safe


def _safe(text):
    return _plaquette_safe(str(text or '').replace('→', '>'))
from profitos.runtime import fr_number, fr_date


def render_declaration_pdf(row, payload, meta, guide, filing_url):
    try:
        from fpdf import FPDF
    except ImportError:
        return None
    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.set_auto_page_break(auto=True, margin=18)
    _multi_cell = pdf.multi_cell

    def multi_cell(*args, **kwargs):
        # Chaque bloc de texte repart de la marge gauche, à la ligne suivante.
        kwargs.setdefault('new_x', 'LMARGIN')
        kwargs.setdefault('new_y', 'NEXT')
        kwargs.setdefault('align', 'L')
        return _multi_cell(*args, **kwargs)
    pdf.multi_cell = multi_cell
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    pdf.set_font('Helvetica', 'B', 16); pdf.set_text_color(17, 24, 39)
    pdf.multi_cell(width, 8, _safe(f"Récapitulatif — {meta['label']} ({row['form']})"), new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', '', 10); pdf.set_text_color(75, 85, 99)
    d = payload.get('declarant') or {}
    pdf.multi_cell(width, 5, _safe(f"{d.get('name') or 'Entreprise'}" + (f" — SIREN {d['siren']}" if d.get('siren') else '')), new_x='LMARGIN', new_y='NEXT')
    if payload.get('due_date'):
        period = (f"Acompte à l'échéance du {fr_date(payload['due_date'])} — exercice de référence du "
                  f"{fr_date(row['period_start'])} au {fr_date(row['period_end'])}")
    else:
        period = f"Période du {fr_date(row['period_start'])} au {fr_date(row['period_end'])}"
    pdf.multi_cell(width, 5, _safe(period + (' — déclaration rectificative' if payload.get('corrective') else '')), new_x='LMARGIN', new_y='NEXT')
    pdf.multi_cell(width, 5, _safe(f"Préparé par ProfitOS le {fr_date(str(payload.get('generated_at') or '')[:10])} — "
                                   f"empreinte {row['payload_hash'][:16]}"))
    pdf.ln(3)

    pdf.set_fill_color(239, 246, 255); pdf.set_text_color(17, 24, 39)
    pdf.set_font('Helvetica', 'B', 10)
    pdf.multi_cell(width, 6, _safe('Comment déposer'), fill=True, new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', '', 9)
    pdf.multi_cell(width, 5, _safe(guide + (f" ({filing_url})" if payload.get('filing') == 'efi' else '')), fill=True, new_x='LMARGIN', new_y='NEXT')
    pdf.multi_cell(width, 5, _safe("Reportez les montants ci-dessous, vérifiez-les et validez la déclaration. Archivez ensuite "
                                   "l'accusé de réception dans ProfitOS : le dépôt n'est confirmé qu'avec ce justificatif."),
                   fill=True)
    pdf.ln(4)

    pdf.set_font('Helvetica', 'B', 9); pdf.set_text_color(17, 24, 39)
    pdf.set_draw_color(226, 232, 240)
    cw = (22, width - 22 - 32, 32)
    for w, h in zip(cw, ('Case', 'Libellé', 'Montant (EUR)')):
        pdf.cell(w, 7, _safe(h), border='B', align='R' if h.startswith('Montant') else 'L')
    pdf.ln()
    pdf.set_font('Helvetica', '', 9)
    is_liasse = row['decl_type'] == 'LIASSE'
    shown = 0
    for b in payload.get('boxes') or []:
        if is_liasse and not b.get('amount'):
            continue
        shown += 1
        y = pdf.get_y()
        if y > pdf.h - 30:
            pdf.add_page(); y = pdf.get_y()
        x = pdf.l_margin
        pdf.set_xy(x + cw[0], y)
        pdf.multi_cell(cw[1], 5, _safe(b.get('label') or ''), new_x='LMARGIN', new_y='NEXT')
        y2 = pdf.get_y()
        pdf.set_xy(x, y)
        pdf.cell(cw[0], 5, _safe(str(b.get('code') or '')))
        pdf.set_xy(x + cw[0] + cw[1], y)
        pdf.cell(cw[2], 5, _safe(fr_number(b.get('amount') or 0, 0)), align='R')
        pdf.set_xy(x, max(y2, y + 5))
        pdf.line(x, pdf.get_y(), x + width, pdf.get_y())
    if is_liasse:
        pdf.set_font('Helvetica', 'I', 8); pdf.set_text_color(107, 114, 128)
        pdf.multi_cell(width, 5, _safe(f"{shown} cases non nulles affichées ; les cases absentes sont à zéro."), new_x='LMARGIN', new_y='NEXT')
    pay = payload.get('payment')
    if pay:
        pdf.ln(3); pdf.set_font('Helvetica', 'B', 10); pdf.set_text_color(17, 24, 39)
        pdf.multi_cell(width, 6, _safe(f"Montant à payer : {fr_number(pay.get('amount') or 0, 0)} EUR"), new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Helvetica', '', 9)
        pdf.multi_cell(width, 5, _safe("Paiement en ligne au moment du dépôt, depuis votre espace professionnel."), new_x='LMARGIN', new_y='NEXT')

    lists = payload.get('annex_lists') or {}
    if lists.get('DAS2'):
        pdf.ln(3); pdf.set_font('Helvetica', 'B', 10)
        pdf.multi_cell(width, 6, _safe('Bénéficiaires (DAS2)'), new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Helvetica', '', 9)
        for r in lists['DAS2']:
            pdf.multi_cell(width, 5, _safe(f"{r.get('name')} — SIRET {r.get('siret') or '?'} — {r.get('address') or ''} — "
                                           f"{r.get('category')} : {fr_number(r.get('amount') or 0, 0)} EUR"))
    if lists.get('DECLOYER') and payload.get('filing') == 'edi':
        pdf.ln(3); pdf.set_font('Helvetica', 'B', 10)
        pdf.multi_cell(width, 6, _safe('Locaux professionnels (DECLOYER)'), new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Helvetica', '', 9)
        for r in lists['DECLOYER']:
            pdf.multi_cell(width, 5, _safe(f"{r.get('invariant') or '?'} — {r.get('address') or ''} : "
                                           f"{fr_number(r.get('rent') or 0, 0)} EUR"))

    pdf.ln(6); pdf.set_font('Helvetica', 'I', 8); pdf.set_text_color(107, 114, 128)
    pdf.multi_cell(width, 4, _safe("ProfitOS calcule et prépare cette déclaration à partir de votre comptabilité ; il ne la "
                                   "transmet pas à l'administration. Vous restez responsable des montants déclarés : faites-les "
                                   "valider par votre expert-comptable en cas de doute."))
    out = pdf.output()
    return bytes(out)

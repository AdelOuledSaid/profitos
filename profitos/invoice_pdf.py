"""Rendu PDF commun des documents de vente : facture, facture d'avoir, devis, bon de
livraison, pro-forma, et page visible du Factur-X.

- Police DejaVu (static/fonts) embarquée : €, œ, espaces insécables et tirets
  s'affichent correctement. À défaut (polices absentes), repli sur Helvetica avec
  translittération latin-1 (€ -> EUR).
- Dates au format jj/mm/aaaa, montants à la française (1 234,56 €), taux « 5,5 % ».
- Adresses sur plusieurs lignes, désignations longues renvoyées à la ligne (jamais
  de texte qui déborde d'une colonne).
- Tableau de TVA par taux, mentions légales et pied de page d'identité du vendeur
  (forme, capital, RCS, SIRET, n° de TVA) sur chaque page.
- Filigrane facultatif (« BROUILLON — document sans valeur de facture », « ANNULÉE »).

Le PDF/A-3 (Factur-X) exige fpdf2 ≥ 2.8.7 : voir pdfa_supported().
"""
from pathlib import Path

from profitos import invoice_amounts as amounts

BASE = Path(__file__).resolve().parents[1]
FONT_REGULAR = BASE / 'static' / 'fonts' / 'DejaVuSans.ttf'
FONT_BOLD = BASE / 'static' / 'fonts' / 'DejaVuSans-Bold.ttf'

INK = (17, 24, 39)
MUTED = (100, 108, 122)
LINE = (220, 224, 230)
FILL = (243, 244, 246)

PAGE_W = 210
MARGIN = 15
CONTENT_W = PAGE_W - 2 * MARGIN


def fpdf_version():
    try:
        import fpdf
        parts = []
        for p in str(getattr(fpdf, '__version__', '0')).split('.')[:3]:
            digits = ''.join(ch for ch in p if ch.isdigit())
            parts.append(int(digits or 0))
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts)
    except ImportError:
        return None


def pdfa_supported():
    """PDF/A-3B + XMP Factur-X : fpdf2 ≥ 2.8.7 (DocumentCompliance) et polices présentes."""
    version = fpdf_version()
    if not version or version < (2, 8, 7):
        return False
    try:
        from fpdf.enums import DocumentCompliance
    except ImportError:
        return False
    return hasattr(DocumentCompliance, 'PDFA_3B') and FONT_REGULAR.is_file() and FONT_BOLD.is_file()


def fr_date(value):
    s = str(value or '')
    if len(s) >= 10 and s[4] == '-' and s[7] == '-':
        return f"{s[8:10]}/{s[5:7]}/{s[0:4]}"
    return s


def money(value):
    v = float(value or 0)
    sign = '-' if v < 0 else ''
    s = f"{abs(v):,.2f}".replace(',', ' ').replace('.', ',')
    return f"{sign}{s} €"


_LATIN1_MAP = {'—': '-', '–': '-', '‘': "'", '’': "'", '“': '"', '”': '"', '…': '...',
               ' ': ' ', ' ': ' ', '€': 'EUR', 'œ': 'oe', 'Œ': 'OE', '≥': '>=', '≤': '<='}


def _new_pdf(watermark=None, footer=None, compliance=None):
    from fpdf import FPDF

    class _Doc(FPDF):
        unicode_ok = False

        def header(self):
            if watermark:
                self.set_font(self.base_font, 'B', 42)
                self.set_text_color(232, 232, 232)
                with self.rotation(35, PAGE_W / 2, 150):
                    width = self.get_string_width(self.t(watermark))
                    self.text(PAGE_W / 2 - width / 2, 150, self.t(watermark))
                self.set_text_color(*INK)
                self.set_xy(MARGIN, MARGIN)

        def footer(self):
            self.set_y(-14)
            self.set_draw_color(*LINE)
            self.line(MARGIN, self.get_y() - 1, PAGE_W - MARGIN, self.get_y() - 1)
            self.set_font(self.base_font, '', 7)
            self.set_text_color(*MUTED)
            if footer:
                self.multi_cell(CONTENT_W - 20, 3.2, self.t(footer), align='L', new_x='RIGHT', new_y='TOP')
            self.set_xy(PAGE_W - MARGIN - 20, -14)
            self.cell(20, 3.2, self.t(f"Page {self.page_no()}/{{nb}}"), align='R')

        def t(self, text):
            text = '' if text is None else str(text)
            if self.unicode_ok:
                return text
            for a, b in _LATIN1_MAP.items():
                text = text.replace(a, b)
            return text.encode('latin-1', errors='replace').decode('latin-1')

    kwargs = {'orientation': 'P', 'unit': 'mm', 'format': 'A4'}
    if compliance is not None:
        kwargs['enforce_compliance'] = compliance
    pdf = _Doc(**kwargs)
    pdf.base_font = 'Helvetica'
    if FONT_REGULAR.is_file() and FONT_BOLD.is_file():
        try:
            pdf.add_font('DejaVu', '', str(FONT_REGULAR))
            pdf.add_font('DejaVu', 'B', str(FONT_BOLD))
            pdf.base_font = 'DejaVu'
            pdf.unicode_ok = True
        except Exception:
            pdf.base_font = 'Helvetica'
    pdf.set_margins(MARGIN, MARGIN, MARGIN)
    pdf.set_auto_page_break(auto=True, margin=22)
    pdf.alias_nb_pages()
    return pdf


def _lines_count(pdf, width, text, line_h):
    """Nombre de lignes qu'occupera text dans une colonne de largeur width."""
    if not text:
        return 1
    try:
        return max(1, len(pdf.multi_cell(width, line_h, pdf.t(text), align='L', dry_run=True, output='LINES')))
    except TypeError:
        return max(1, len(pdf.multi_cell(width, line_h, pdf.t(text), split_only=True)))


def _block(pdf, x, y, width, title, lines, title_size=8):
    """Bloc d'adresse : titre en petites capitales puis lignes. Retourne la hauteur."""
    pdf.set_xy(x, y)
    if title:
        pdf.set_font(pdf.base_font, 'B', title_size)
        pdf.set_text_color(*MUTED)
        pdf.cell(width, 4.5, pdf.t(title.upper()), new_x='LEFT', new_y='NEXT')
    pdf.set_text_color(*INK)
    first = True
    for line in lines:
        if not line:
            continue
        pdf.set_x(x)
        pdf.set_font(pdf.base_font, 'B' if first else '', 10 if first else 9)
        pdf.multi_cell(width, 4.6 if first else 4.2, pdf.t(line), align='L', new_x='LEFT', new_y='NEXT')
        first = False
    return pdf.get_y() - y


def address_lines(text):
    return [l.strip() for l in str(text or '').replace('\r', '').split('\n') if l.strip()]


def _payment_block(pdf, pay):
    """Encadré « Régler cette facture » : QR code de virement SEPA, IBAN, référence, lien."""
    matrix = pay.get('qr_matrix')
    size = 30 if matrix else 0
    height = max(size, 26) + 6
    if pdf.get_y() + height > pdf.h - pdf.b_margin:
        pdf.add_page()
    y0 = pdf.get_y() + 1
    pdf.set_draw_color(205, 212, 226)
    pdf.rect(MARGIN, y0, CONTENT_W, height)
    x_text = MARGIN + 4
    if matrix:
        n = len(matrix)
        cell = size / (n + 2)
        ox, oy = MARGIN + 3, y0 + 3
        pdf.set_fill_color(0, 0, 0)
        for r, row in enumerate(matrix):
            c = 0
            while c < n:
                if row[c]:
                    start = c
                    while c < n and row[c]:
                        c += 1
                    pdf.rect(ox + (start + 1) * cell, oy + (r + 1) * cell, (c - start) * cell, cell, style='F')
                else:
                    c += 1
        x_text = MARGIN + size + 7
    width = CONTENT_W - (x_text - MARGIN) - 3
    pdf.set_xy(x_text, y0 + 3)
    pdf.set_font(pdf.base_font, 'B', 9.5)
    pdf.cell(width, 5, pdf.t(f"Régler cette facture — reste à payer : {money(pay['balance'])}"), new_x='LEFT', new_y='NEXT')
    pdf.set_font(pdf.base_font, '', 8.5)
    lines = []
    if pay.get('iban'):
        lines.append(f"Virement : IBAN {pay['iban']}" + (f" — BIC {pay['bic']}" if pay.get('bic') else ''))
        lines.append(f"Référence à indiquer : {pay['reference']}")
        if matrix:
            lines.append("Scannez le QR code avec l'application de votre banque : le virement est pré-rempli.")
    if pay.get('link'):
        lines.append(f"Payer en ligne : {pay['link']}")
    for line in lines:
        pdf.set_x(x_text)
        pdf.multi_cell(width, 4.2, pdf.t(line), align='L', new_x='LEFT', new_y='NEXT')
    pdf.set_y(y0 + height + 3)
    pdf.set_x(MARGIN)


def render_document(doc, watermark=None, compliance=None, attachments=None, xmp=None, title=None):
    """Rend un document de vente. doc : dict (voir build_* dans routes/invoicing.py).
    Retourne les octets du PDF, ou None si fpdf2 est absent."""
    try:
        import fpdf  # noqa: F401
    except ImportError:
        return None
    pdf = _new_pdf(watermark=watermark, footer=doc.get('footer'), compliance=compliance)
    if title:
        pdf.set_title(title)
    pdf.set_creator('ProfitOS')
    seller = doc.get('seller') or {}
    pdf.add_page()

    # --- En-tête : vendeur à gauche, titre du document à droite -------------------------
    top = pdf.get_y()
    seller_lines = [seller.get('name')] + address_lines(seller.get('address'))
    city_line = ' '.join(p for p in (seller.get('postal_code'), seller.get('city')) if p)
    if city_line and city_line not in seller_lines:
        seller_lines.append(city_line)
    if seller.get('country') and seller.get('country') != 'FR':
        seller_lines.append(seller.get('country'))
    if seller.get('siret'):
        seller_lines.append(f"SIRET {seller.get('siret')}")
    if seller.get('vat_number'):
        seller_lines.append(f"N° TVA intracommunautaire : {seller.get('vat_number')}")
    h_left = _block(pdf, MARGIN, top, 95, None, seller_lines)

    pdf.set_xy(MARGIN + 100, top)
    title_size = float(doc.get('title_size') or 15)
    pdf.set_font(pdf.base_font, 'B', title_size)
    pdf.set_text_color(*INK)
    pdf.multi_cell(CONTENT_W - 100, title_size * 0.43, pdf.t(doc.get('title', '')), align='R', new_x='LEFT', new_y='NEXT')
    if doc.get('subtitle'):
        pdf.set_x(MARGIN + 100)
        pdf.set_font(pdf.base_font, '', 9)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(CONTENT_W - 100, 4.5, pdf.t(doc['subtitle']), align='R', new_x='LEFT', new_y='NEXT')
    pdf.set_text_color(*INK)
    for label, value in doc.get('meta') or []:
        if not value:
            continue
        pdf.set_x(MARGIN + 100)
        pdf.set_font(pdf.base_font, '', 9)
        pdf.multi_cell(CONTENT_W - 100, 4.6, pdf.t(f"{label} : {value}"), align='R', new_x='LEFT', new_y='NEXT')
    y = max(top + h_left, pdf.get_y()) + 6

    # --- Client / livraison ----------------------------------------------------------------
    buyer = doc.get('buyer') or {}
    buyer_lines = [buyer.get('name')] + address_lines(buyer.get('address'))
    if buyer.get('country') and buyer.get('country') != 'FR':
        buyer_lines.append(f"Pays : {buyer.get('country')}")
    if buyer.get('siren'):
        buyer_lines.append(f"SIREN {buyer.get('siren')}")
    if buyer.get('vat_number'):
        buyer_lines.append(f"N° TVA intracommunautaire : {buyer.get('vat_number')}")
    delivery = address_lines(doc.get('delivery_address'))
    h1 = _block(pdf, MARGIN + 100, y, CONTENT_W - 100, doc.get('buyer_title', 'Facturé à'), buyer_lines)
    h2 = 0
    if delivery:
        h2 = _block(pdf, MARGIN, y, 90, doc.get('delivery_title', 'Adresse de livraison'), [buyer.get('name')] + delivery)
    y = y + max(h1, h2) + 6

    # --- Informations de l'opération -------------------------------------------------------
    pdf.set_xy(MARGIN, y)
    for label, value in doc.get('details') or []:
        if not value:
            continue
        pdf.set_x(MARGIN)
        pdf.set_font(pdf.base_font, 'B', 9)
        label_txt = pdf.t(f"{label} : ")
        lw = pdf.get_string_width(label_txt) + 1
        pdf.cell(lw, 4.8, label_txt)
        pdf.set_font(pdf.base_font, '', 9)
        pdf.multi_cell(CONTENT_W - lw, 4.8, pdf.t(value), align='L', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(3)

    # --- Lignes ------------------------------------------------------------------------------
    columns = doc.get('columns') or [('Désignation', 82, 'L'), ('Qté', 18, 'R'), ('PU HT', 28, 'R'),
                                     ('TVA', 18, 'R'), ('Total HT', 34, 'R')]
    line_h = 4.6

    def table_header():
        pdf.set_fill_color(*FILL)
        pdf.set_font(pdf.base_font, 'B', 9)
        pdf.set_x(MARGIN)
        for label, width, align in columns:
            pdf.cell(width, 7, pdf.t(label), fill=True, align=align)
        pdf.ln(7)

    table_header()
    pdf.set_font(pdf.base_font, '', 9)
    for row in doc.get('rows') or []:
        n = _lines_count(pdf, columns[0][1] - 1, row[0], line_h)
        height = n * line_h + 1.5
        if pdf.get_y() + height > pdf.h - 24:
            pdf.add_page()
            table_header()
            pdf.set_font(pdf.base_font, '', 9)
        y0 = pdf.get_y()
        pdf.set_xy(MARGIN, y0 + 0.75)
        pdf.multi_cell(columns[0][1] - 1, line_h, pdf.t(row[0]), align='L', new_x='RIGHT', new_y='TOP')
        x = MARGIN + columns[0][1]
        for (label, width, align), value in zip(columns[1:], row[1:]):
            pdf.set_xy(x, y0 + 0.75)
            pdf.cell(width, line_h, pdf.t(value), align=align)
            x += width
        pdf.set_draw_color(*LINE)
        pdf.line(MARGIN, y0 + height, PAGE_W - MARGIN, y0 + height)
        pdf.set_xy(MARGIN, y0 + height)
    pdf.ln(4)

    # --- TVA par taux (à gauche) et totaux (à droite) --------------------------------------
    y_totals = pdf.get_y()
    if pdf.get_y() > pdf.h - 70:
        pdf.add_page()
        y_totals = pdf.get_y()
    breakdown = doc.get('vat_breakdown') or []
    if breakdown:
        pdf.set_xy(MARGIN, y_totals)
        pdf.set_fill_color(*FILL)
        pdf.set_font(pdf.base_font, 'B', 8.5)
        for label, width in (('Taux de TVA', 26), ('Base HT', 32), ('Montant TVA', 32)):
            pdf.cell(width, 6, pdf.t(label), fill=True, align='R' if label != 'Taux de TVA' else 'L')
        pdf.ln(6)
        pdf.set_font(pdf.base_font, '', 8.5)
        for b in breakdown:
            pdf.set_x(MARGIN)
            pdf.cell(26, 5.2, pdf.t(b.get('label') or amounts.fr_rate(b['rate'])))
            pdf.cell(32, 5.2, pdf.t(money(b['base'])), align='R')
            pdf.cell(32, 5.2, pdf.t(money(b['vat'])), align='R')
            pdf.ln(5.2)
    y_left_end = pdf.get_y()
    pdf.set_xy(MARGIN + 100, y_totals)
    for label, value, strong in doc.get('totals') or []:
        pdf.set_x(MARGIN + 100)
        pdf.set_font(pdf.base_font, 'B' if strong else '', 11 if strong else 9.5)
        pdf.cell(44, 6.5 if strong else 5.6, pdf.t(label), align='L')
        pdf.cell(CONTENT_W - 144, 6.5 if strong else 5.6, pdf.t(value), align='R')
        pdf.ln(6.5 if strong else 5.6)
    pdf.set_y(max(y_left_end, pdf.get_y()) + 5)

    # --- Mentions légales et notes --------------------------------------------------------
    mentions = [m for m in (doc.get('mentions') or []) if m]
    if mentions:
        pdf.set_font(pdf.base_font, '', 8)
        pdf.set_text_color(*MUTED)
        for m in mentions:
            pdf.set_x(MARGIN)
            pdf.multi_cell(CONTENT_W, 3.8, pdf.t(m), align='L', new_x='LMARGIN', new_y='NEXT')
        pdf.set_text_color(*INK)
        pdf.ln(2)
    pay = doc.get('payment')
    if pay:
        _payment_block(pdf, pay)
    if doc.get('notes'):
        pdf.set_font(pdf.base_font, '', 8.5)
        pdf.set_x(MARGIN)
        pdf.multi_cell(CONTENT_W, 4, pdf.t(doc['notes']), align='L', new_x='LMARGIN', new_y='NEXT')
    if doc.get('signature_box'):
        pdf.ln(6)
        pdf.set_font(pdf.base_font, '', 9)
        pdf.set_x(MARGIN + 100)
        pdf.multi_cell(CONTENT_W - 100, 18, pdf.t(doc['signature_box']), border=1, align='L', new_x='LMARGIN', new_y='NEXT')

    for att in attachments or []:
        pdf.embed_file(**att)
    if xmp and hasattr(pdf, 'set_xmp_metadata'):
        try:
            pdf.set_xmp_metadata(xmp)
        except Exception:
            pass
    return bytes(pdf.output())

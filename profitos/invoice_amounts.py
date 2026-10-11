"""Calcul des montants d'une facture : source unique de vérité.

Les mêmes chiffres doivent apparaître dans la base, le PDF, le XML Factur-X et
l'écriture comptable. Règles appliquées (EN 16931 / pratique française) :

- chaque montant de ligne est arrondi au centime, au demi supérieur (3 × 12,345 €
  = 37,035 € -> 37,04 €), jamais au « demi pair » de round() ;
- la base HT de chaque taux est la somme des lignes déjà arrondies ;
- la TVA de chaque taux est calculée une seule fois sur cette base, puis arrondie
  (BR-CO-17) ; la TVA de la facture est la somme des TVA par taux (BR-CO-14) ;
- TTC = HT + TVA (BR-CO-15).

Le prix unitaire est conservé avec au plus 4 décimales (saisie), la quantité avec
au plus 4 décimales.
"""
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

CENT = Decimal('0.01')
NATURES = ('biens', 'services')


def dec(value):
    """Decimal exact à partir d'un float/str (via str pour éviter 0.1+0.2). Accepte la
    saisie française : « 1 234,5 » (espaces, espaces insécables, virgule)."""
    if isinstance(value, Decimal):
        return value
    raw = str(value if value not in (None, '') else 0)
    for ch in (' ', '\u00a0', '\u202f', '€'):
        raw = raw.replace(ch, '')
    try:
        return Decimal(raw.replace(',', '.'))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def parse_number(value, default=None):
    """Nombre saisi (« 12,345 », « 1 000 ») -> float, ou default si illisible/non fini."""
    raw = str(value if value is not None else '').strip()
    for ch in (' ', '\u00a0', '\u202f', '€', '%'):
        raw = raw.replace(ch, '')
    if not raw:
        return default
    try:
        d = Decimal(raw.replace(',', '.'))
    except (InvalidOperation, ValueError):
        return default
    if not d.is_finite():
        return default
    return float(d)


def money(value):
    """Arrondi commercial au centime (demi supérieur), renvoyé en float."""
    return float(dec(value).quantize(CENT, rounding=ROUND_HALF_UP))


def quantize(value, places=4):
    q = Decimal(1).scaleb(-places)
    return float(dec(value).quantize(q, rounding=ROUND_HALF_UP))


def line_total(qty, unit_price):
    return money(dec(qty) * dec(unit_price))


def rate_key(rate):
    """Clé stable pour un taux (5.5 et '5,5' -> 5.5)."""
    return float(dec(rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def item_nature(item, default_nature='services'):
    nature = (item.get('nature') if isinstance(item, dict) else None) or default_nature
    if nature not in NATURES:
        nature = 'services' if default_nature not in NATURES else default_nature
    return nature


def normalize_items(items):
    """Recalcule line_total (arrondi au centime) de chaque ligne. Les lignes
    historiques enregistrées avec un total non arrondi (37,035) sont corrigées à la
    lecture, sans modifier les factures émises en base."""
    out = []
    for it in items or []:
        x = dict(it)
        x['qty'] = quantize(x.get('qty', 1), 4)
        x['unit_price'] = quantize(x.get('unit_price', 0), 4)
        x['vat_rate'] = rate_key(x.get('vat_rate', 0))
        x['line_total'] = line_total(x['qty'], x['unit_price'])
        out.append(x)
    return out


def vat_breakdown(items):
    """Liste triée par taux : {'rate', 'base', 'vat'} (bases et TVA arrondies)."""
    bases = {}
    for it in items or []:
        r = rate_key(it.get('vat_rate', 0))
        bases[r] = bases.get(r, Decimal(0)) + dec(money(it.get('line_total', 0)))
    out = []
    for r in sorted(bases):
        base = bases[r].quantize(CENT, rounding=ROUND_HALF_UP)
        vat = (base * dec(r) / Decimal(100)).quantize(CENT, rounding=ROUND_HALF_UP)
        out.append({'rate': r, 'base': float(base), 'vat': float(vat)})
    return out


def totals(items):
    """(HT, TVA, TTC) arrondis, cohérents avec vat_breakdown."""
    bd = vat_breakdown(items)
    subtotal = sum((dec(b['base']) for b in bd), Decimal(0))
    vat = sum((dec(b['vat']) for b in bd), Decimal(0))
    return float(subtotal), float(vat), float(subtotal + vat)


def nature_rate_breakdown(items, default_nature='services'):
    """Répartit base et TVA de chaque taux entre biens et services.

    Retourne une liste {'nature','rate','base','vat'} dont, pour chaque taux, la
    somme des TVA est exactement la TVA du taux (le dernier groupe prend le reste
    d'arrondi). Sert à l'écriture comptable (707/706, TVA exigible ou en attente)."""
    per_rate = {b['rate']: b for b in vat_breakdown(items)}
    groups = {}
    for it in items or []:
        if it.get('deposit_deduction'):
            nature = 'deduction'
        else:
            nature = item_nature(it, default_nature)
        r = rate_key(it.get('vat_rate', 0))
        groups.setdefault(r, {}).setdefault(nature, Decimal(0))
        groups[r][nature] += dec(money(it.get('line_total', 0)))
    out = []
    for r in sorted(groups):
        rate_vat = dec(per_rate[r]['vat'])
        rate_base = dec(per_rate[r]['base'])
        natures = [n for n in ('biens', 'services', 'deduction') if n in groups[r]]
        allocated = Decimal(0)
        for i, n in enumerate(natures):
            base = groups[r][n].quantize(CENT, rounding=ROUND_HALF_UP)
            if i == len(natures) - 1:
                vat = rate_vat - allocated
            elif rate_base:
                vat = (rate_vat * base / rate_base).quantize(CENT, rounding=ROUND_HALF_UP)
            else:
                vat = Decimal(0)
            allocated += vat
            out.append({'nature': n, 'rate': r, 'base': float(base), 'vat': float(vat)})
    return out


def fr_qty(value):
    """Quantité lisible : 1 -> « 1 », 2.5 -> « 2,5 »."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value or '')
    s = f"{v:,.4f}".rstrip('0').rstrip('.')
    return s.replace(',', ' ').replace('.', ',')


def fr_rate(value):
    """Taux lisible : 10.0 -> « 10 % », 5.5 -> « 5,5 % »."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value or '')
    s = f"{v:.2f}".rstrip('0').rstrip('.')
    return s.replace('.', ',') + ' %'


def fr_price(value):
    """Prix unitaire : 2 décimales minimum, jusqu'à 4 si nécessaires (12,345)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value or '')
    s = f"{v:,.4f}"
    head, tail = s.split('.')
    tail = tail.rstrip('0')
    if len(tail) < 2:
        tail = tail.ljust(2, '0')
    return head.replace(',', ' ') + ',' + tail

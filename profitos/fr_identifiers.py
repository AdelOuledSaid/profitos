"""Contrôles des identifiants français (SIREN/SIRET, n° de TVA, IBAN, code postal)
et découpage d'adresse. Utilisés par le profil entreprise, les fiches clients et le
contrôle des mentions obligatoires des factures."""
import re


def _luhn_ok(digits):
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def siren_is_valid(siren):
    """SIREN : 9 chiffres, clé de Luhn."""
    s = re.sub(r'\D', '', str(siren or ''))
    return len(s) == 9 and _luhn_ok(s)


def siret_is_valid(siret):
    """SIRET : 14 chiffres, clé de Luhn sur le SIRET et sur le SIREN. Exception connue :
    les établissements de La Poste (SIREN 356000000), dont la somme des chiffres du
    SIRET est un multiple de 5."""
    s = re.sub(r'\D', '', str(siret or ''))
    if len(s) != 14:
        return False
    if s.startswith('356000000'):
        # Établissements de La Poste : somme des chiffres multiple de 5 (le siège,
        # 356 000 000 00048, respecte en plus la clé de Luhn).
        return sum(int(ch) for ch in s) % 5 == 0 or _luhn_ok(s)
    return _luhn_ok(s) and _luhn_ok(s[:9])


def fr_vat_is_valid(vat, siren=None):
    """N° TVA intracommunautaire. Pour la France : FR + clé (2 caractères) + SIREN, la
    clé numérique valant (12 + 3 × (SIREN mod 97)) mod 97. Les autres pays de l'UE sont
    contrôlés sur leur forme (préfixe pays + 2 à 13 caractères)."""
    v = re.sub(r'[\s.\-]', '', str(vat or '')).upper()
    if not v:
        return False
    if v.startswith('FR'):
        m = re.fullmatch(r'FR([0-9A-Z]{2})(\d{9})', v)
        if not m or not siren_is_valid(m.group(2)):
            return False
        if siren and re.sub(r'\D', '', str(siren))[:9] != m.group(2):
            return False
        key = m.group(1)
        if key.isdigit():
            return int(key) == (12 + 3 * (int(m.group(2)) % 97)) % 97
        return True  # clé alphanumérique (nouveaux assujettis) : forme seule vérifiable
    return bool(re.fullmatch(r'(AT|BE|BG|CY|CZ|DE|DK|EE|EL|ES|FI|HR|HU|IE|IT|LT|LU|LV|MT|NL|PL|PT|RO|SE|SI|SK|XI)[0-9A-Z+*]{2,13}', v))


def iban_is_valid(iban):
    """IBAN : longueur minimale et clé ISO 7064 mod 97-10."""
    v = re.sub(r'\s', '', str(iban or '')).upper()
    if not re.fullmatch(r'[A-Z]{2}\d{2}[A-Z0-9]{10,30}', v):
        return False
    if v.startswith('FR') and len(v) != 27:
        return False
    rearranged = v[4:] + v[:4]
    numeric = ''.join(str(int(ch, 36)) for ch in rearranged)
    return int(numeric) % 97 == 1


def fr_postal_code_is_valid(code, country='FR'):
    c = re.sub(r'\s', '', str(code or ''))
    if (country or 'FR').upper() != 'FR':
        return bool(c) and len(c) <= 12
    return bool(re.fullmatch(r'\d{5}', c))


def split_address(text):
    """Découpe une adresse libre en lignes ; repère « 69002 Lyon » (code postal + ville)."""
    lines = [l.strip() for l in re.split(r'[\r\n]+', str(text or '')) if l.strip()]
    if len(lines) == 1 and ',' in lines[0]:
        lines = [p.strip() for p in lines[0].split(',') if p.strip()]
    postcode, city, street = '', '', []
    for l in lines:
        m = re.fullmatch(r'(?:([A-Z]{2})[- ])?(\d{4,5})\s+(.+)', l)
        if m and not postcode:
            postcode, city = m.group(2), m.group(3).strip()
        else:
            street.append(l)
    return {'lines': lines, 'street': street, 'postcode': postcode, 'city': city}

"""Générateur de QR codes (ISO/IEC 18004) sans dépendance : mode octet, correction
d'erreur M, versions 1 à 15 (jusqu'à 412 octets), choix automatique du masque.

Sert au QR code de virement SEPA (norme EPC069-12) imprimé sur les factures : le client
le scanne avec l'application de sa banque et le virement est pré-rempli (bénéficiaire,
IBAN, montant, référence de la facture).
"""

# Version -> (codewords d'erreur par bloc, [(nombre de blocs, codewords de données par bloc)]), niveau M.
_BLOCKS_M = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]), 5: (24, [(2, 43)]),
    6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]), 9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]), 11: (30, [(1, 50), (4, 51)]), 12: (22, [(6, 36), (2, 37)]),
    13: (22, [(8, 37), (1, 38)]), 14: (24, [(4, 40), (5, 41)]), 15: (24, [(5, 41), (5, 42)]),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
          9: [6, 26, 46], 10: [6, 28, 50], 11: [6, 30, 54], 12: [6, 32, 58], 13: [6, 34, 62],
          14: [6, 26, 46, 66], 15: [6, 26, 48, 70]}
_REMAINDER = {v: (7 if 2 <= v <= 6 else 3 if v >= 14 else 0) for v in range(1, 16)}
_EC_LEVEL_M = 0b00

# Corps de Galois GF(256), polynôme 0x11D.
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(n):
    g = [1]
    for i in range(n):
        g = [a ^ b for a, b in zip(g + [0], [0] + [_gf_mul(c, _EXP[i]) for c in g])]
    return g


def _rs_remainder(data, n):
    gen = _rs_generator(n)
    rem = list(data) + [0] * n
    for i in range(len(data)):
        coef = rem[i]
        if coef:
            for j, g in enumerate(gen):
                rem[i + j] ^= _gf_mul(g, coef)
    return rem[len(data):]


def _capacity(version):
    _ec, groups = _BLOCKS_M[version]
    return sum(n * k for n, k in groups)


def _choose_version(nbytes):
    for v in range(1, 16):
        header_bits = 4 + (8 if v <= 9 else 16)
        if header_bits + 8 * nbytes <= 8 * _capacity(v):
            return v
    raise ValueError('données trop longues pour un QR code (412 octets au plus)')


def _codewords(data, version):
    cap = _capacity(version)
    bits = [0, 1, 0, 0]                                  # mode octet
    count_len = 8 if version <= 9 else 16
    bits += [(len(data) >> i) & 1 for i in range(count_len - 1, -1, -1)]
    for b in data:
        bits += [(b >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, cap * 8 - len(bits))            # terminateur
    bits += [0] * (-len(bits) % 8)
    words = [int(''.join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = [0xEC, 0x11]
    while len(words) < cap:
        words.append(pad[(len(words) - len(bits) // 8) % 2])
    ec_len, groups = _BLOCKS_M[version]
    blocks, pos = [], 0
    for n, k in groups:
        for _ in range(n):
            blocks.append(words[pos:pos + k])
            pos += k
    ecs = [_rs_remainder(b, ec_len) for b in blocks]
    out = []
    for i in range(max(len(b) for b in blocks)):
        out += [b[i] for b in blocks if i < len(b)]
    for i in range(ec_len):
        out += [e[i] for e in ecs]
    return out


def _bch(value, poly, total_bits):
    shift = poly.bit_length() - 1
    v = value << shift
    while v.bit_length() >= poly.bit_length():
        v ^= poly << (v.bit_length() - poly.bit_length())
    return (value << shift) | v


def _base_matrix(version):
    size = 17 + 4 * version
    m = [[None] * size for _ in range(size)]

    def finder(r, c):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                rr, cc = r + dr, c + dc
                if 0 <= rr < size and 0 <= cc < size:
                    inside = 0 <= dr <= 6 and 0 <= dc <= 6
                    dark = inside and (dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4))
                    m[rr][cc] = dark
    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)
    for i in range(8, size - 8):                          # motifs de synchronisation
        m[6][i] = m[i][6] = (i % 2 == 0)
    pos = _ALIGN[version]
    for r in pos:
        for c in pos:
            if (r, c) in ((pos[0], pos[0]), (pos[0], pos[-1]), (pos[-1], pos[0])):
                continue                                   # coins occupés par les motifs de position
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    m[r + dr][c + dc] = max(abs(dr), abs(dc)) != 1
    m[size - 8][8] = True                                 # module sombre
    for i in range(9):                                    # réservation des zones de format
        if m[8][i] is None:
            m[8][i] = False
        if m[i][8] is None:
            m[i][8] = False
    for i in range(8):
        if m[8][size - 1 - i] is None:
            m[8][size - 1 - i] = False
        if m[size - 1 - i][8] is None:
            m[size - 1 - i][8] = False
    if version >= 7:                                      # informations de version
        info = _bch(version, 0x1F25, 18)
        for i in range(18):
            bit = bool((info >> i) & 1)
            a, b = i // 3, size - 11 + i % 3
            m[a][b] = bit
            m[b][a] = bit
    return m


_MASKS = [
    lambda r, c: (r + c) % 2 == 0, lambda r, c: r % 2 == 0, lambda r, c: c % 3 == 0, lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0, lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0, lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _place(base, codewords, version, mask):
    size = len(base)
    m = [row[:] for row in base]
    bits = []
    for w in codewords:
        bits += [(w >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * _REMAINDER[version]
    idx, upward = 0, True
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for c in (col, col - 1):
                if base[r][c] is None:
                    bit = bits[idx] if idx < len(bits) else 0
                    idx += 1
                    m[r][c] = bool(bit) ^ _MASKS[mask](r, c)
        upward = not upward
        col -= 2
    fmt = _bch((_EC_LEVEL_M << 3) | mask, 0x537, 15) ^ 0x5412
    for i in range(15):
        bit = bool((fmt >> i) & 1)
        # copie autour du motif haut-gauche
        if i < 6:
            m[i][8] = bit
        elif i < 8:
            m[i + 1][8] = bit
        else:
            m[size - 15 + i][8] = bit
        # copie le long des motifs haut-droit et bas-gauche
        if i < 8:
            m[8][size - 1 - i] = bit
        elif i < 9:
            m[8][7] = bit
        else:
            m[8][14 - i] = bit
    m[size - 8][8] = True
    return m


def _penalty(m):
    size = len(m)
    score = 0
    for lines in (m, list(zip(*m))):
        for line in lines:
            run, prev = 0, None
            for v in line:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, prev = 1, v
            if run >= 5:
                score += run - 2
            s = ''.join('1' if v else '0' for v in line)
            score += 40 * (s.count('10111010000') + s.count('00001011101'))
    for r in range(size - 1):
        for c in range(size - 1):
            if m[r][c] == m[r + 1][c] == m[r][c + 1] == m[r + 1][c + 1]:
                score += 3
    dark = sum(sum(1 for v in row if v) for row in m)
    score += abs(dark * 100 // (size * size) - 50) // 5 * 10
    return score


def matrix(data):
    """Matrice du QR code (liste de listes de booléens, True = module sombre)."""
    if isinstance(data, str):
        data = data.encode('utf-8')
    version = _choose_version(len(data))
    words = _codewords(data, version)
    base = _base_matrix(version)
    candidates = [_place(base, words, version, k) for k in range(8)]
    return min(candidates, key=_penalty)


def svg(data, module=4, border=4, label=None):
    """QR code en SVG autonome (aucun script, compatible avec la politique de sécurité).
    module=None : largeur 100 % du conteneur (page web)."""
    m = matrix(data)
    n = len(m) + 2 * border
    path = ''.join(f'M{c + border},{r + border}h1v1h-1z' for r, row in enumerate(m) for c, v in enumerate(row) if v)
    title = f'<title>{label}</title>' if label else ''
    size = f'width="{n * module}" height="{n * module}"' if module else 'width="100%"'
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" {size} '
            f'shape-rendering="crispEdges" role="img">{title}<rect width="{n}" height="{n}" fill="#fff"/>'
            f'<path d="{path}" fill="#000"/></svg>')


# ------------------------------------------------------------- virement SEPA
def epc_payload(name, iban, amount, reference, bic=''):
    """Contenu du QR code de virement SEPA (EPC069-12, version 002, UTF-8)."""
    iban = ''.join(str(iban or '').split()).upper()
    name = ' '.join(str(name or '').split())[:70]
    if not name or not iban:
        raise ValueError('bénéficiaire et IBAN obligatoires')
    amount = round(float(amount), 2)
    if not 0.01 <= amount <= 999999999.99:
        raise ValueError('montant hors limites')
    reference = ' '.join(str(reference or '').split())[:140]
    lines = ['BCD', '002', '1', 'SCT', ''.join(str(bic or '').split()).upper()[:11], name, iban,
             f'EUR{amount:.2f}', '', '', reference]
    return '\n'.join(lines)

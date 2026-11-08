"""v494.2 — Double authentification : QR code dessiné par Veqalio et nouvelle clé à la demande."""
import re

import pytest

from profitos import qr, totp
from support_v4xx import app, flashes, post, signup  # noqa: F401


def secret_of(page):
    return re.search(r'secret=([A-Z2-7]+)', page).group(1)


def test_page_affiche_un_qr_code_local(app):
    client = signup(app)[0]
    page = client.get('/compte/2fa').get_data(as_text=True)
    assert '<svg xmlns="http://www.w3.org/2000/svg"' in page and 'QR code de la double authentification' in page
    assert 'Saisir la clé à la main' in page                   # la saisie manuelle reste possible
    assert 'http' not in page.split('<svg', 1)[1].split('</svg>', 1)[0].replace('http://www.w3.org/2000/svg', '')


def test_qr_code_contient_exactement_l_uri():
    cv2 = pytest.importorskip('cv2')
    np = pytest.importorskip('numpy')
    uri = totp.provisioning_uri(totp.new_secret(), 'dirigeant+test@example.com')
    m = qr.matrix(uri)
    img = np.full((len(m) + 8, len(m) + 8), 255, np.uint8)
    for r, row in enumerate(m):
        for c, v in enumerate(row):
            if v:
                img[r + 4, c + 4] = 0
    img = cv2.resize(img, None, fx=10, fy=10, interpolation=cv2.INTER_NEAREST)
    decoded, _pts, _raw = cv2.QRCodeDetector().detectAndDecode(img)
    assert decoded == uri


def test_recharger_garde_la_cle_mais_le_bouton_en_genere_une_nouvelle(app):
    client = signup(app)[0]
    first = secret_of(client.get('/compte/2fa').get_data(as_text=True))
    assert secret_of(client.get('/compte/2fa').get_data(as_text=True)) == first
    post(client, '/compte/2fa', {'action': 'regenerate'})
    assert any("Nouvelle clé générée" in m for m in flashes(client))
    second = secret_of(client.get('/compte/2fa').get_data(as_text=True))
    assert second != first
    # l'ancienne clé est refusée, la nouvelle active la double authentification
    post(client, '/compte/2fa', {'code': totp.totp(first)})
    assert any('Code incorrect' in m for m in flashes(client))
    r = post(client, '/compte/2fa', {'code': totp.totp(second)})
    assert re.findall(r'<li>([a-z0-9]{5}-[a-z0-9]{5})</li>', r.get_data(as_text=True))

"""v491 — Nouvelle marque Veqalio : nom et logo visibles partout, identifiants techniques
inchangés (en-têtes de webhooks, variables d'environnement, cookie, domaine)."""
import json
import re
from pathlib import Path

from test_v470_encaissement import app, signup  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
VISIBLE = re.compile(r"(?<![A-Za-z_/.-])(ProfitOS|PROFITOS)(?![A-Za-z0-9_/-])")  # « ProfitOS/1.0 » : User-Agent technique conservé


def tracked(*prefixes):
    return [f for p in prefixes for f in (ROOT / p).rglob('*') if f.is_file() and '__pycache__' not in f.parts]


def test_aucun_ancien_nom_visible_dans_les_ecrans_et_messages():
    bad = []
    for f in tracked('templates', 'static', 'profitos'):
        if f.suffix not in ('.html', '.py', '.js', '.css', '.json', '.txt'):
            continue
        for i, line in enumerate(f.read_text(encoding='utf-8').splitlines(), 1):
            if VISIBLE.search(line) and 'profitos2026' not in line.lower() and 'devient Veqalio' not in line:
                bad.append(f'{f.relative_to(ROOT)}:{i}')
    assert not bad, bad


def test_identifiants_techniques_conserves():
    src = (ROOT / 'profitos' / 'webhooks_outbound.py').read_text(encoding='utf-8')
    assert "'X-ProfitOS-Signature'" in src and "'X-ProfitOS-Event'" in src   # contrat des intégrations clients
    assert 'PROFITOS-' in (ROOT / 'profitos' / 'sepa.py').read_text(encoding='utf-8')
    config = (ROOT / 'profitos' / 'config.py').read_text(encoding='utf-8')
    assert "'__Host-profitos_session'" in config                            # personne n'est déconnecté


def test_logo_et_icones(app):
    cl, org = signup(app)
    html = cl.get('/').get_data(as_text=True)
    assert 'class="brand-mark"' in html and 'Veqal<span>io</span>' in html
    assert 'icons/favicon.svg' in html
    manifest = json.loads((ROOT / 'static' / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['name'] == 'Veqalio' and manifest['short_name'] == 'Veqalio'
    for name in ('icon-192.png', 'icon-512.png', 'apple-touch-icon.png', 'favicon.svg'):
        assert (ROOT / 'static' / 'icons' / name).stat().st_size > 300


def test_page_de_connexion_et_mot_de_passe_au_nom_du_produit_refuse(app):
    from profitos.account_security import password_policy_error
    html = app.test_client().get('/login').get_data(as_text=True)
    assert 'Veqal<span>io</span>' in html and 'ProfitOS' not in html
    assert password_policy_error('veqalio2026') and password_policy_error('Veqalio2027')

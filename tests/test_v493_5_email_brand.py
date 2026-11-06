"""v493.5 — Les e-mails portent le nom Veqalio (plus d'en-tête « ProfitOS »)."""
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / 'templates'


def test_aucun_gabarit_ne_porte_l_ancien_nom():
    for p in TEMPLATES.rglob('*.html'):
        text = p.read_text(encoding='utf-8')
        assert 'Profit<span' not in text, p.name
    for name in ('email_transactional.html', 'email_demo_confirmation.html', 'email_demo_admin.html'):
        assert 'Veqal<span style="color:#3ddc84;">io</span>' in (TEMPLATES / name).read_text(encoding='utf-8')

"""Génère templates/help_content.html à partir de docs/guide-utilisateur.md.

Le guide utilisateur est rédigé en Markdown (docs/guide-utilisateur.md). Ce script le
convertit une fois en HTML, au moment de la mise à jour du guide : l'application n'a
donc pas besoin de la bibliothèque markdown en production.

    pip install markdown        # une seule fois, sur le poste de rédaction
    python scripts/build_help.py
"""
import re
import sys
import unicodedata
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'docs' / 'guide-utilisateur.md'
OUT = ROOT / 'templates' / 'help_content.html'
TOC = ROOT / 'templates' / 'help_toc.html'


def slugify(value, separator='-'):
    t = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().lower()
    t = re.sub(r'^\d+\.\s*', '', t)  # « 5. Facturation clients » -> « facturation-clients »
    return re.sub(r'[^a-z0-9]+', separator, t).strip(separator)


def build():
    md = SRC.read_text(encoding='utf-8')
    lines = md.splitlines()
    # Titre et ligne d'auteur du document de travail : remplacés par l'en-tête de la page.
    if lines and lines[0].startswith('# '):
        lines = lines[1:]
    lines = [l for l in lines if not re.match(r'^\w{3} \d{1,2}, \d{4} · @', l.strip())]
    md = '\n'.join(lines)
    # Schéma du cycle d'une facture : inséré depuis un gabarit SVG.
    md = re.sub(r'^.*embedded content: cycle d.une facture.*$', '{% include "help_invoice_cycle.html" %}', md, flags=re.M)
    # Adresse du support : celle configurée sur le serveur.
    md = md.replace("à l'adresse indiquée dans les mentions légales du site.",
                    "à <a href=\"mailto:{{ support_email() }}\">{{ support_email() }}</a>.")
    md = md.replace('&#91;', '[').replace('\\]', ']')
    html = markdown.markdown(md, extensions=['tables', 'toc', 'sane_lists'],
                             extension_configs={'toc': {'slugify': slugify}}, output_format='html5')
    html = html.replace('<p>{% include "help_invoice_cycle.html" %}</p>',
                        '<figure class="help-figure">{% include "help_invoice_cycle.html" %}'
                        '<figcaption>Cycle d\'une facture : 6 étapes, une correction par avoir</figcaption></figure>')
    html = html.replace('<table>', '<div class="help-table"><table>').replace('</table>', '</table></div>')
    header = '{# Fichier généré par scripts/build_help.py depuis docs/guide-utilisateur.md — ne pas modifier à la main. #}\n'
    OUT.write_text(header + html + '\n', encoding='utf-8')
    items = re.findall(r'<h2 id="([^"]+)">(.*?)</h2>', html)
    toc = ''.join(f'<li><a href="#{i}">{t}</a></li>' for i, t in items)
    TOC.write_text(header + f'<ol class="help-toc-list">{toc}</ol>\n', encoding='utf-8')
    return OUT


if __name__ == '__main__':
    print(build(), file=sys.stdout)

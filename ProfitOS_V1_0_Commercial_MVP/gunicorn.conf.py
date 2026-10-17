"""Configuration gunicorn ProfitOS (chargée automatiquement depuis la racine).

Les options de capacité (--workers 2 --threads 4 --timeout 120 ...) sont sur la
ligne de commande de render.yaml / Procfile / railway.json. Ce fichier ne fait
qu'une chose : un journal d'accès SANS chaîne de requête et sans jetons.

Plusieurs URL portent un secret dans le chemin (réinitialisation de mot de
passe, vérification d'email, facture/devis publics, export comptable,
désinscription) : les segments ressemblant à un jeton sont remplacés par
« [jeton] », la chaîne de requête et le Referer ne sont jamais écrits.
"""
import re

_TOKEN_SEGMENT = re.compile(r'/[A-Za-z0-9_\-\.]{20,}(?=/|$)')

# h=IP, U=chemin sans query string, s=statut, B=octets, M=durée en ms
access_log_format = '%(h)s "%(m)s %(U)s %(H)s" %(s)s %(B)s %(M)sms req=%({x-request-id}o)s'


def scrub_path(path):
    """Retire la chaîne de requête et masque les segments de type jeton."""
    path = (path or '').split('?', 1)[0]
    return _TOKEN_SEGMENT.sub('/[jeton]', path)


try:
    from gunicorn.glogging import Logger as _GunicornLogger

    class SafeAccessLogger(_GunicornLogger):
        def atoms(self, resp, req, environ, request_time):
            atoms = super().atoms(resp, req, environ, request_time)
            atoms['U'] = scrub_path(atoms.get('U'))
            atoms['r'] = f"{atoms.get('m', '-')} {atoms['U']} {atoms.get('H', '-')}"
            atoms['q'] = ''
            atoms['f'] = '-'
            return atoms

    logger_class = SafeAccessLogger
except ImportError:  # gunicorn absent (tests, Windows) : rien à configurer
    pass

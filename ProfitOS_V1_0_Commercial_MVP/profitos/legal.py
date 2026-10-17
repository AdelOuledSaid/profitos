"""Identité légale de l'éditeur de ProfitOS (mentions légales, CGV, confidentialité).

Les informations viennent des variables d'environnement LEGAL_* afin d'être renseignées
une seule fois sur Render, sans modifier le code. En production, le preflight refuse de
déployer tant que les mentions obligatoires (LCEN art. 6 III) ne sont pas fournies.
"""
import os
import re

# Champ -> (variable d'environnement, libellé, obligatoire)
FIELDS = {
    'company_name': ('LEGAL_COMPANY_NAME', 'raison sociale', True),
    'legal_form': ('LEGAL_FORM', 'forme juridique', True),
    'share_capital': ('LEGAL_SHARE_CAPITAL', 'capital social', False),
    'siren': ('LEGAL_SIREN', 'SIREN', True),
    'rcs_city': ('LEGAL_RCS_CITY', 'ville du RCS', False),
    'vat_number': ('LEGAL_VAT_NUMBER', 'n° de TVA intracommunautaire', False),
    'address': ('LEGAL_ADDRESS', 'adresse du siège', True),
    'director': ('LEGAL_PUBLICATION_DIRECTOR', 'directeur de la publication', True),
    'email': ('LEGAL_CONTACT_EMAIL', 'e-mail de contact', True),
    'phone': ('LEGAL_PHONE', 'téléphone', False),
    'privacy_email': ('LEGAL_PRIVACY_EMAIL', 'contact données personnelles', False),
    'host_name': ('LEGAL_HOST_NAME', "nom de l'hébergeur", False),
    'host_address': ('LEGAL_HOST_ADDRESS', "adresse de l'hébergeur", False),
    'host_contact': ('LEGAL_HOST_CONTACT', "contact de l'hébergeur", False),
    'court_city': ('LEGAL_COURT_CITY', 'tribunal compétent', False),
}

DEFAULTS = {
    'host_name': 'Render Services, Inc.',
    'host_address': '525 Brannan Street, Suite 300, San Francisco, CA 94107, États-Unis',
    'host_contact': 'https://render.com/contact',
}

# Sous-traitants réellement susceptibles de recevoir des données (RGPD art. 28 et 13).
SUBPROCESSORS = [
    ('Render', "Hébergement de l'application, de la base de données et des documents", 'Serveurs en Union européenne (Francfort) ; société établie aux États-Unis'),
    ('Stripe', "Paiement et facturation de l'abonnement ProfitOS", 'Irlande / États-Unis'),
    ('Resend', 'Envoi des e-mails (vérification, factures, relances, rapports)', 'États-Unis'),
    ('Anthropic', 'Lecture automatique des factures et justificatifs déposés, si la fonction est activée', 'États-Unis'),
    ('Powens', 'Synchronisation bancaire (open banking), si vous connectez une banque', 'France'),
    ('WeInvoice', 'Émission et réception des factures électroniques, si vous activez la facturation électronique', 'France'),
    ('Twilio', 'Envoi des SMS de relance, si vous les utilisez', 'États-Unis'),
    ('Google, Dropbox', "Import de factures depuis votre stockage en ligne, si vous le connectez", 'États-Unis'),
    ('Sentry', "Suivi des erreurs techniques (sans contenu de vos factures)", 'États-Unis'),
]

RETENTION = [
    ('Compte utilisateur et données de votre organisation', "Pendant toute la durée de l'abonnement ; suppression définitive 14 jours après une demande de suppression du compte"),
    ('Factures, écritures et justificatifs importés', "Pendant l'abonnement. Exportables à tout moment (FEC, PDF, CSV) : la conservation légale de 10 ans (C. com. art. L123-22) reste de votre responsabilité après la résiliation"),
    ('Journaux de sécurité et d\'activité', '12 mois'),
    ('Demandes de démonstration', '3 ans après le dernier contact'),
    ("Factures de l'abonnement ProfitOS", '10 ans (obligation comptable)'),
    ('Sauvegardes chiffrées', '35 jours maximum'),
]


# Champs exigés en pré-lancement (société pas encore immatriculée) : l'éditeur reste
# identifiable (nom, directeur de publication, contact), sans SIREN ni adresse de siège.
PRELAUNCH_REQUIRED = ('company_name', 'director', 'email')


def is_prelaunch(env=None):
    """Mode pré-lancement : site consultable avant l'immatriculation, sans vente.
    Inscriptions limitées à PRELAUNCH_ALLOWED_EMAILS, paiement en ligne fermé."""
    env = os.environ if env is None else env
    return (env.get('PROFITOS_PRELAUNCH') or '').strip().lower() in ('1', 'true', 'oui', 'yes', 'on')


def prelaunch_allowed_emails(env=None):
    env = os.environ if env is None else env
    return {e.strip().lower() for e in (env.get('PRELAUNCH_ALLOWED_EMAILS') or '').split(',') if e.strip()}


def legal_identity():
    data = {}
    for key, (env, _label, _req) in FIELDS.items():
        data[key] = (os.environ.get(env) or DEFAULTS.get(key, '')).strip()
    if not data['privacy_email']:
        data['privacy_email'] = data['email']
    # « ProfitOS SAS » + forme « SAS » s'affichait « ProfitOS SAS, SAS ».
    form = data['legal_form']
    if form and data['company_name'].upper().endswith(' ' + form.upper()):
        data['company_name'] = data['company_name'][:-len(form)].rstrip(' ,')
    data['siren'] = re.sub(r'\s', '', data['siren']) if siren_valid(data['siren']) else data['siren']
    data['prelaunch'] = is_prelaunch()
    data['missing'] = missing_legal_fields()
    return data


def missing_legal_fields(env=None):
    """Variables LEGAL_* obligatoires encore vides (noms des variables)."""
    env = os.environ if env is None else env
    prelaunch = is_prelaunch(env)
    return [var for key, (var, label, required) in FIELDS.items()
            if (key in PRELAUNCH_REQUIRED if prelaunch else required) and not (env.get(var) or '').strip()]


def siren_valid(value):
    """SIREN réel : 9 chiffres et clé de Luhn (exception La Poste, 356 000 000 et ses
    établissements : somme des chiffres multiple de 5). Rejette les numéros factices
    comme 123456789 ou 000000000."""
    digits = re.sub(r'\s', '', value or '')
    if not re.fullmatch(r'\d{9}', digits) or len(set(digits)) == 1:
        return False
    if digits == '356000000':
        return True
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def legal_identity_errors(env=None):
    """Valeurs LEGAL_* renseignées mais manifestement fausses (bloquant au déploiement)."""
    env = os.environ if env is None else env
    errors = []
    siren = (env.get('LEGAL_SIREN') or '').strip()
    if siren and not siren_valid(siren):
        errors.append('LEGAL_SIREN invalide : un SIREN comporte 9 chiffres avec une clé de contrôle '
                      '(vérifiez-le sur annuaire-entreprises.data.gouv.fr).')
    address = (env.get('LEGAL_ADDRESS') or '').strip()
    if address and siren and not re.search(r'\b\d{5}\b', address):
        errors.append('LEGAL_ADDRESS incomplète : indiquez le code postal et la ville du siège '
                      '(ex. 15 rue Exemple, 75001 Paris).')
    return errors

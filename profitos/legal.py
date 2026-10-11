"""Identité légale de l'éditeur de ProfitOS (mentions légales, CGV, confidentialité).

Les informations viennent des variables d'environnement LEGAL_* afin d'être renseignées
une seule fois sur Render, sans modifier le code. En production, le preflight refuse de
déployer tant que les mentions obligatoires (LCEN art. 6 III) ne sont pas fournies.
"""
import os

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


def legal_identity():
    data = {}
    for key, (env, _label, _req) in FIELDS.items():
        data[key] = (os.environ.get(env) or DEFAULTS.get(key, '')).strip()
    if not data['privacy_email']:
        data['privacy_email'] = data['email']
    data['missing'] = missing_legal_fields()
    return data


def missing_legal_fields():
    return [label for key, (env, label, required) in FIELDS.items()
            if required and not (os.environ.get(env) or '').strip()]

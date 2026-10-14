"""Libellés français des codes internes affichés à l'utilisateur.

Les journaux (activité, sécurité), les formules et les rôles sont stockés sous forme de codes
techniques (``LOGIN_SUCCESS``, ``MULTI``, ``OWNER``…). Ils ne doivent jamais apparaître tels
quels dans l'interface : ce module fournit le libellé français correspondant.
"""
import os

PLAN_LABELS = {
    'TRIAL': 'Essai gratuit', 'STARTER': 'Starter', 'PRO': 'Pro', 'BUSINESS': 'Business',
    'MULTI': 'Multi-sociétés', 'FREE': 'Gratuit',
}

ROLE_SHORT_LABELS = {
    'OWNER': 'Propriétaire', 'ADMIN': 'Administrateur', 'COMPTABLE': 'Comptable',
    'COMMERCIAL': 'Commercial', 'MEMBER': 'Membre', 'LECTURE': 'Lecture seule',
}

ROLE_DESCRIPTIONS = {
    'OWNER': "Accès complet, y compris l'abonnement et la suppression du compte.",
    'ADMIN': "Accès complet sauf l'abonnement : équipe, paramètres, facturation, comptabilité.",
    'COMPTABLE': 'Facturation, relances clients, dépenses, comptabilité et banque. Pas les marchés publics.',
    'COMMERCIAL': "Marchés publics et centre d'actions uniquement.",
    'MEMBER': "Tous les modules métier, sans l'équipe, les paramètres ni l'abonnement.",
    'LECTURE': 'Consultation des modules, sans aucune modification.',
}

SECURITY_EVENT_LABELS = {
    '2FA_DISABLED': 'Double authentification désactivée',
    '2FA_ENABLED': 'Double authentification activée',
    '2FA_RECOVERY_REGENERATED': 'Codes de secours régénérés',
    'ACCOUNT_LOCKED': 'Compte verrouillé',
    'BILLING_CHECKOUT': "Paiement de l'abonnement",
    'BILLING_DUPLICATE_SUBSCRIPTION': 'Abonnement en double évité',
    'BILLING_PORTAL': 'Accès au portail de facturation',
    'CABINET_ACCESS_DENIED': 'Accès cabinet refusé',
    'EMAIL_CHANGED': 'Adresse e-mail modifiée',
    'EMAIL_CHANGE_REQUESTED': "Changement d'adresse e-mail demandé",
    'EMAIL_QUOTA': "Quota d'e-mails atteint",
    'EMAIL_VERIFY': 'Adresse e-mail vérifiée',
    'ENTITY_ACCESS_DENIED': 'Accès à une société refusé',
    'GDPR_EXPORT': 'Export des données (RGPD)',
    'LOGIN_2FA_FAILED': 'Code de double authentification incorrect',
    'LOGIN_FAILED': 'Connexion échouée',
    'LOGIN_LOCKED': 'Connexion bloquée (trop de tentatives)',
    'LOGIN_SUCCESS': 'Connexion réussie',
    'LOGOUT': 'Déconnexion',
    'ORG_2FA_REQUIRED': 'Double authentification exigée',
    'ORG_DELETION_CANCELLED': 'Suppression du compte annulée',
    'ORG_DELETION_SCHEDULED': 'Suppression du compte programmée',
    'PASSWORD_CHANGED': 'Mot de passe modifié',
    'PASSWORD_RESET': 'Mot de passe réinitialisé',
    'PASSWORD_RESET_REQUEST': 'Réinitialisation du mot de passe demandée',
    'PERSONAL_DATA_EXPORT': 'Export des données personnelles',
    'PLAN_LIMIT': 'Limite de la formule atteinte',
    'REAUTH_FAILED': 'Confirmation du mot de passe échouée',
    'ROLE_AREA_DENIED': 'Accès refusé (rôle)',
    'ROLE_READ_ONLY_DENIED': 'Modification refusée (lecture seule)',
    'SIGNUP_EXISTING_EMAIL': 'Inscription avec une adresse déjà utilisée',
    'SWAN_TRANSFER_DENIED': 'Virement refusé',
    'TEAM_INVITE': 'Invitation envoyée',
    'TEAM_INVITE_ACCEPTED': 'Invitation acceptée',
    'TEAM_INVITE_REVOKED': 'Invitation révoquée',
    'TEAM_REMOVE': "Membre retiré de l'équipe",
    'TEAM_ROLE_UPDATE': 'Rôle modifié',
}

OUTCOME_LABELS = {'SUCCESS': 'Réussi', 'FAILURE': 'Échec', 'INFO': 'Information', 'DENIED': 'Refusé',
                  'BLOCKED': 'Bloqué', 'WARNING': 'Avertissement'}

TARGET_LABELS = {'account': 'Compte', 'organization': 'Organisation', 'org': 'Organisation',
                 'team': 'Équipe', 'billing': 'Abonnement', 'password': 'Mot de passe',
                 'email': 'Adresse e-mail', '2fa': 'Double authentification'}

# Catégorie d'une ligne du journal d'activité, d'après le préfixe du code. La description
# (déjà rédigée en français) reste le texte principal.
ACTIVITY_PREFIXES = (
    ('INVOICE', 'Facturation'), ('DEPOSIT_INVOICE', 'Facturation'), ('FINAL_INVOICE', 'Facturation'),
    ('CREDIT_NOTE', 'Facturation'), ('PROFORMA', 'Facturation'), ('QUOTE', 'Devis'),
    ('DELIVERY_NOTE', 'Bons de livraison'), ('PURCHASE', 'Achats'), ('SEPA', 'Achats'),
    ('API_', 'API'), ('TEAM', 'Équipe'), ('ACCOUNTANT', 'Équipe'), ('LOGIN', 'Connexion'),
    ('LOGOUT', 'Connexion'), ('SIGNUP', 'Inscription'), ('ONBOARDING', 'Démarrage'),
    ('SETTINGS', 'Paramètres'), ('WEBHOOK', 'Paramètres'), ('SWAN', 'Compte pro'),
    ('GOCARDLESS', 'Prélèvement'), ('CASH', 'Trésorerie'), ('MONEY_HUNTER', 'Consultation'),
    ('FINANCIAL_BRAIN', 'Consultation'), ('DECISION', 'Simulation'), ('ACCOUNTING', 'Comptabilité'),
    ('FEC', 'Comptabilité'), ('CUTOFF', 'Comptabilité'), ('LOAN', 'Comptabilité'),
    ('FINANCE_LEASE', 'Comptabilité'), ('FIXED_ASSET', 'Comptabilité'), ('PAYROLL', 'Comptabilité'),
    ('PLAQUETTE', 'Comptabilité'), ('REVIEW', 'Révision'), ('EREPORTING', 'Facturation électronique'),
    ('EXPENSE', 'Notes de frais'), ('BANK', 'Banque'), ('ECOMMERCE', 'E-commerce'),
    ('CLOUD_STORAGE', 'Stockage'), ('ORG', 'Organisation'), ('ENTITY', 'Sociétés'),
    ('GDPR', 'Données personnelles'), ('AUDIT', 'Journal'), ('STATUS_CHANGE', 'Statut'),
    ('ACTION', 'Actions'), ('PARTNER', 'Partenaires'), ('INTEGRATION', 'Intégrations'),
    ('CABINET', 'Cabinet'), ('PUSH', 'Application mobile'), ('ACCOUNT', 'Compte'),
)


def plan_label(code):
    return PLAN_LABELS.get(str(code or '').upper(), code or '')


def role_label(code):
    return ROLE_SHORT_LABELS.get(str(code or '').upper(), code or '')


def role_description(code):
    return ROLE_DESCRIPTIONS.get(str(code or '').upper(), '')


def security_event_label(code):
    return SECURITY_EVENT_LABELS.get(str(code or '').upper(), str(code or '').replace('_', ' ').capitalize())


def outcome_label(code):
    return OUTCOME_LABELS.get(str(code or '').upper(), code or '')


def target_label(value):
    if not value:
        return '—'
    return TARGET_LABELS.get(str(value).lower(), value)


def activity_label(code):
    code = str(code or '').upper()
    for prefix, label in ACTIVITY_PREFIXES:
        if code.startswith(prefix):
            return label
    return 'Activité'


def env_flag(name):
    return (os.environ.get(name) or '').strip().lower() in ('1', 'true', 'yes', 'on')


def gocardless_enabled():
    """GoCardless reste masqué tant que l'intégration n'est pas activée explicitement :
    jeton global unique, mandat signé côté marchand et paiements non suivis en V1."""
    return env_flag('GOCARDLESS_ENABLED')


def supplier_inbox_enabled():
    """Adresse mail fournisseurs : affichée seulement si le serveur de réception est branché."""
    return env_flag('SUPPLIER_INBOX_WEBHOOK_ENABLED')


PAGE_TITLES = {
    'home': 'Tableau de bord', 'login': 'Connexion', 'signup': 'Créer un compte', 'forgot_password': 'Mot de passe oublié',
    'reset_password': 'Nouveau mot de passe', 'login_two_factor': 'Double authentification', 'onboarding': 'Démarrage',
    'account_security': 'Sécurité du compte', 'two_factor_setup': 'Double authentification',
    'accountant_collaboration': 'Collaboration avec le cabinet', 'accounting_chart': 'Plan comptable',
    'accounting_closure': 'Clôture des comptes', 'accounting_fec_export': 'Export FEC', 'accounting_journal_detail': 'Journal',
    'accounting_journals_view': 'Journaux', 'accounting_lettrage': 'Lettrage', 'actions': "Centre d'actions",
    'analytical_axes_list': 'Comptabilité analytique', 'analytical_profitability': 'Rentabilité analytique',
    'api_keys': 'Clés API', 'banking_rules': 'Règles de catégorisation', 'banking': 'Banques connectées', 'billing': 'Abonnement',
    'billing_success': 'Abonnement', 'cabinet_documents': 'Documents du cabinet', 'cabinet_portfolio': 'Portefeuille clients',
    'cabinet_time_list': 'Temps passés', 'cabinet_time_new': 'Saisir un temps', 'calendar_view': 'Échéancier',
    'cash_forecast': 'Flux projetés', 'cash_intelligence': 'Trésorerie et scénarios', 'cloud_storage_settings': 'Stockage en ligne',
    'company': 'Profil entreprise', 'cutoff_list': 'Séparation des exercices', 'cutoff_new': 'Nouvelle écriture de séparation',
    'decision_simulator': 'Simulateur de décision', 'delete_account': 'Supprimer mon compte',
    'delivery_note_detail': 'Bon de livraison', 'delivery_note_new': 'Nouveau bon de livraison', 'delivery_notes_list': 'Bons de livraison',
    'detail': 'Détail', 'ecommerce_settings': 'Shopify et WooCommerce', 'entities_consolidated': 'Vue consolidée',
    'entities_list': 'Sociétés', 'entity_detail': 'Société', 'entity_new': 'Nouvelle société',
    'expense_report_detail': 'Note de frais', 'expense_reports_list': 'Notes de frais', 'features_overview': 'Fonctionnalités',
    'finance_lease_detail': 'Crédit-bail', 'finance_lease_new': 'Nouveau crédit-bail', 'finance_leases_list': 'Crédit-bail',
    'financial_brain': 'Diagnostic financier', 'fiscal_workpapers': 'Dossiers fiscaux', 'fixed_asset_detail': 'Immobilisation',
    'fixed_assets_list': 'Immobilisations', 'grow': 'Marchés publics', 'impact': 'Gains réalisés', 'integrations': 'Intégrations comptables',
    'invoicing_client_detail': 'Client', 'invoicing_client_new': 'Nouveau client', 'invoicing_clients': 'Clients',
    'invoicing_credit_detail': 'Avoir', 'invoicing_credit_new': 'Nouvel avoir', 'invoicing_detail': 'Facture',
    'invoicing_edit': 'Modifier la facture', 'invoicing_electronic_diagnostic': 'Facturation électronique',
    'invoicing_list': 'Factures', 'invoicing_new': 'Nouvelle facture', 'invoicing_quote_detail': 'Devis',
    'invoicing_quote_new': 'Nouveau devis', 'invoicing_quotes': 'Devis', 'invoicing_receivables': 'Créances clients',
    'help_center': 'Guide utilisateur', 'legal_notice': 'Mentions légales', 'privacy_policy': 'Politique de confidentialité', 'terms_of_use': "Conditions générales d'utilisation",
    'terms_of_sale': 'Conditions générales de vente', 'loan_detail': 'Emprunt', 'loan_new': 'Nouvel emprunt', 'loans_list': 'Emprunts',
    'margin_watch': 'Suivi des marges', 'mileage_rate_settings': 'Barème kilométrique', 'money_hunter': 'Gisements de cash',
    'outbound_webhooks': 'Webhooks', 'partners': 'Partenaires', 'payroll_imports_list': 'Paie', 'plaquette': 'Comptes annuels',
    'portfolio': 'Vue consolidée', 'pricing': 'Tarifs', 'purchase_analytics': 'Pilotage des dépenses', 'purchase_budgets': 'Budgets',
    'purchase_detail': 'Facture fournisseur', 'purchase_edit': 'Modifier la facture fournisseur', 'purchase_inbox_settings': 'Boîte mail fournisseurs',
    'purchase_list': 'Achats', 'purchase_new': 'Nouvelle facture fournisseur', 'purchase_order_detail': 'Bon de commande',
    'purchase_order_new': 'Nouveau bon de commande', 'purchase_orders_list': 'Bons de commande', 'purchase_recurring': 'Charges récurrentes',
    'purchase_sepa_batch': 'Virements SEPA groupés', 'recover': 'Relances clients', 'recurring_invoices': 'Factures récurrentes',
    'referral': 'Parrainage', 'request_demo': 'Demander une démo', 'review_detail': 'Révision', 'reviews_list': 'Révision comptable',
    'save': 'Économies', 'security_events': 'Événements de sécurité', 'settings': 'Paramètres', 'supplier_debts': 'Dettes fournisseurs',
    'supplier_detail': 'Fournisseur', 'supplier_new': 'Nouveau fournisseur', 'supplier_payment_forecast': 'Prévision des paiements fournisseurs',
    'swan_settings': 'Compte professionnel', 'team': 'Équipe', 'team_entity_access': 'Accès par société', 'team_invitation': 'Invitation',
    'team_performance': "Performance de l'équipe", 'upload_bank_statement': 'Importer un relevé bancaire',
    'upload_expenses': 'Importer des dépenses', 'upload_invoices': 'Importer des factures', 'vat_summary': 'Synthèse TVA',
    'weekly_report_preview': 'Rapport hebdomadaire', 'public_profit_audit': 'Audit de rentabilité', 'subscription_inactive': 'Abonnement inactif',
}


def page_title(endpoint, explicit=None):
    """« <Page> — ProfitOS » (une page = un titre distinct, utile aux lecteurs d'écran et aux onglets)."""
    label = explicit or PAGE_TITLES.get(endpoint or '')
    return f"{label} — ProfitOS" if label else 'ProfitOS'


def register(app):
    app.jinja_env.globals['page_title'] = page_title
    for name, fn in (('plan_label', plan_label), ('role_label', role_label), ('role_description', role_description),
                     ('security_event_label', security_event_label), ('outcome_label', outcome_label),
                     ('target_label', target_label), ('activity_label', activity_label)):
        app.jinja_env.filters[name] = fn
        app.jinja_env.globals[name] = fn
    app.jinja_env.globals['gocardless_enabled'] = gocardless_enabled
    app.jinja_env.globals['supplier_inbox_enabled'] = supplier_inbox_enabled

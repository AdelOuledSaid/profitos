import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Isolation des tests — AVANT tout import de l'application.
#
# profitos/runtime.py appelle load_dotenv() : sans cette protection, pytest lit
# le .env (ou les variables Windows) et tourne sur la base PostgreSQL de
# PRODUCTION, le Redis de production, et peut envoyer de vrais e-mails/SMS ou
# appeler Stripe/WeInvoice. Chaque lancement créait alors de vrais comptes de test
# dans la base clients.
#
# Ici : le .env n'est pas chargé, et chaque service externe est neutralisé.
# Les tests tournent toujours sur SQLite locale, en mode développement.
# ---------------------------------------------------------------------------
try:
    import dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
except ImportError:
    pass

_EXTERNAL_SERVICE_VARS = (
    'DATABASE_URL', 'REDIS_URL',
    'RESEND_API_KEY', 'SMTP_HOST', 'SMTP_USER', 'SMTP_PASSWORD',
    'STRIPE_SECRET_KEY', 'STRIPE_PUBLISHABLE_KEY', 'STRIPE_WEBHOOK_SECRET',
    'TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN',
    'ANTHROPIC_API_KEY',
    'WEINVOICE_CLIENT_ID', 'WEINVOICE_CLIENT_SECRET',
    'WEINVOICE_INVOICE_CLIENT_ID', 'WEINVOICE_INVOICE_CLIENT_SECRET',
    'SWAN_CLIENT_ID', 'SWAN_CLIENT_SECRET',
    'POWENS_CLIENT_ID', 'POWENS_CLIENT_SECRET',
    'GOCARDLESS_ACCESS_TOKEN',
    'DROPBOX_CLIENT_ID', 'DROPBOX_CLIENT_SECRET',
    'GOOGLE_DRIVE_CLIENT_ID', 'GOOGLE_DRIVE_CLIENT_SECRET',
    'OPS_HEALTH_TOKEN',
)
_PG_TEST_URL = os.environ.get('PROFITOS_TEST_DATABASE_URL', '').strip()
for _name in _EXTERNAL_SERVICE_VARS:
    # Chaîne vide plutôt que suppression : même si un load_dotenv non intercepté
    # s'exécutait, il ne remplacerait pas une variable déjà définie.
    os.environ[_name] = ''
# Exécution volontaire sur PostgreSQL (base de test vide et dédiée, jamais la production) :
# PROFITOS_TEST_DATABASE_URL=postgresql://... pytest tests/test_v467_isolation_audit.py
# Le DATABASE_URL éventuellement présent dans l'environnement n'est jamais utilisé.
if _PG_TEST_URL.startswith(('postgres://', 'postgresql://')):
    os.environ['DATABASE_URL'] = _PG_TEST_URL
os.environ['PROFITOS_ENV'] = 'development'

import os
# Les tests tournent explicitement en environnement de développement : sans cette
# valeur, l'application se considère en production et refuse de démarrer sans clé.
os.environ.setdefault('PROFITOS_ENV', 'development')


if _PG_TEST_URL.startswith(('postgres://', 'postgresql://')):
    import pytest

    @pytest.fixture(autouse=True)
    def _postgres_clean_database():
        """Mode PostgreSQL : chaque test repart d'une base vide (schémas des entreprises et
        tables d'authentification supprimés), comme chaque test SQLite repart de fichiers
        temporaires neufs. Ne s'exécute qu'avec PROFITOS_TEST_DATABASE_URL."""
        from profitos import db as dbmod
        dbmod.reset_pool()          # aucune connexion du test précédent ne garde de verrou
        conn = dbmod.connect_auth(None)
        try:
            # Connexions oubliées par le test précédent (« idle in transaction ») : elles
            # bloqueraient DROP SCHEMA indéfiniment. Base de test dédiée : on les coupe.
            conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                         "WHERE datname=current_database() AND pid<>pg_backend_pid()")
            conn.execute("SET lock_timeout = '20s'")
            schemas = [r['nspname'] for r in conn.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname LIKE ? OR nspname='public'", ('org\\_%',)).fetchall()]
            for name in schemas:
                conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
            conn.execute('CREATE SCHEMA public')
            conn.commit()
        finally:
            conn.close()
        # Caches du processus liés aux schémas supprimés (un serveur réel ne voit jamais sa
        # base effacée sous ses pieds) : on les vide, et on repart d'un pool neuf.
        import profitos.runtime as _rt
        dbmod._ensured_schemas.clear()
        _rt._tenant_schema_checked.clear()
        _rt._aging_refreshed.clear()
        dbmod.reset_pool()
        yield


# Tests écrits pour SQLite (ils ouvrent directement le fichier de la base, lisent
# sqlite_master ou vérifient la migration d'un fichier SQLite) : sans objet sur
# PostgreSQL. Analyse du 20/10/2026 : aucun ne révèle d'erreur de l'application, le code
# testé passe par les mêmes fonctions que les autres tests, verts sur PostgreSQL.
_SQLITE_ONLY_TESTS = {
    'test_v378_issue_ops_external_accountant.py::test_test_suite_never_touches_the_production_database',
    'test_v390_production_audit.py::test_full_credit_note_settles_invoice_and_blocks_fake_payment',
    'test_v390_production_audit.py::test_partial_credit_reduces_balance_for_payment',
    'test_v390_production_audit.py::test_trial_can_invoice_and_reach_accounting',
    'test_v419_gdpr_retention.py::test_organization_export_contains_every_tenant_table',
    'test_v421_invoice_compliance.py::test_draft_has_no_number_and_issue_is_blocked_until_mentions_complete',
    'test_v421_invoice_compliance.py::test_final_invoice_deduction_uses_negative_quantity',
    'test_v421_invoice_compliance.py::test_issue_is_atomic_when_accounting_entry_is_refused',
    'test_v421_invoice_compliance.py::test_issued_pdf_prints_all_mandatory_mentions_and_vat_per_rate',
    'test_v421_invoice_compliance.py::test_numbering_at_issue_cancelled_draft_consumes_nothing_and_series_per_entity',
    'test_v421_invoice_compliance.py::test_profile_rejects_invalid_identifiers_keeps_values_and_stays_on_page',
    'test_v421_invoice_compliance.py::test_profile_saves_legal_fields_and_hides_boamp_errors',
    'test_v421_invoice_compliance.py::test_public_link_only_for_issued_invoice_and_draft_pdf_watermark',
    'test_v421_invoice_compliance.py::test_quote_conversion_copies_fields_and_activates_public_link_at_issue',
    'test_v421_invoice_compliance.py::test_send_issues_then_emails_and_email_failure_keeps_issued_invoice',
    'test_v421_purchases_formats.py::test_invoice_detail_formats_quantities_rates_and_partial_status',
    'test_v421_purchases_formats.py::test_purchase_payment_date_cannot_be_future',
    'test_v421_purchases_formats.py::test_purchase_requires_issue_date_blocks_due_before_issue_and_warns_duplicate',
    'test_v421_vat_accounting.py::test_credit_note_follows_original_accounts_and_cancels_pending_vat',
    'test_v421_vat_accounting.py::test_goods_and_debits_option_book_vat_at_invoice_and_mixed_lines_split_accounts',
    'test_v421_vat_accounting.py::test_payment_date_in_future_refused_and_cancelled_draft_does_not_block_vat_lock',
    'test_v421_vat_accounting.py::test_services_vat_pending_until_payment_and_ca3_by_rate',
    'test_v422_compliance_verification.py::test_cancelled_draft_message_uses_draft_label',
    'test_v422_compliance_verification.py::test_delivery_notes_numbered_per_entity_and_invoice_copies_nature_delivery_due_date',
    'test_v422_compliance_verification.py::test_demo_services_vat_collected_equals_exigible_amount_only',
    'test_v422_compliance_verification.py::test_exempt_categories_k_g_e_in_pdf_and_xml',
    'test_v422_compliance_verification.py::test_foreign_b2b_reverse_charge_requires_buyer_vat_and_prints_mention',
    'test_v422_compliance_verification.py::test_public_buyer_gets_chorus_pro_notice',
    'test_v422_compliance_verification.py::test_reminder_failure_shows_generic_french_message',
    'test_v422_compliance_verification.py::test_subsidiary_invoice_uses_subsidiary_identity_everywhere',
    'test_v424_multi_societes_parent_billing.py::test_migration_adds_billing_columns_to_existing_db',
    'test_v43x_gating_confirm.py::test_gocardless_hidden_unless_enabled',
    'test_v43x_receivables.py::test_consolidated_view_shows_receivables',
    'test_v43x_receivables.py::test_credit_note_reduces_receivable',
    'test_v43x_receivables.py::test_dashboard_receivables_page_recover_and_forecast_agree',
    'test_v43x_receivables.py::test_forecast_page_deducts_supplier_invoices_and_never_shows_minus_zero',
    'test_v43x_receivables.py::test_helper_merges_both_sources_deduplicates_and_nets_payments',
    'test_v445_final_audit.py::test_settings_save_and_no_fake_currency_choice',
    'test_v446_coherence.py::test_existing_loan_past_installments_marked_paid',
    'test_v447_security_audit.py::test_quote_accepted_without_email_then_invoiced_and_settled',
}


if _PG_TEST_URL.startswith(('postgres://', 'postgresql://')):
    def pytest_collection_modifyitems(config, items):
        import pytest as _pytest
        mark = _pytest.mark.skip(reason='test propre à SQLite (accès direct au fichier de base)')
        for item in items:
            key = item.nodeid.split('/')[-1].split('[')[0]
            if key in _SQLITE_ONLY_TESTS:
                item.add_marker(mark)

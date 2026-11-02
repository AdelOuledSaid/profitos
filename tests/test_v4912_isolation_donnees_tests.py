"""v491.2 — Les tests n'écrivent jamais dans la base de développement du dépôt : chaque
lancement repart d'un dossier temporaire neuf (sinon les données s'accumulent d'un lancement
à l'autre et des contraintes d'unicité échouent)."""
from pathlib import Path

import profitos.runtime as rt
from profitos import storage

ROOT = Path(__file__).resolve().parent.parent


def test_bases_et_documents_hors_du_depot():
    import os
    pg = os.environ.get('PROFITOS_TEST_DATABASE_URL', '').startswith(('postgres://', 'postgresql://'))
    # Sur PostgreSQL (base de test dédiée), les fichiers SQLite ne servent pas : seuls les
    # documents sont concernés.
    for path in ((storage.UPLOAD_ROOT,) if pg else (rt.AUTH_DB, rt.TENANTS, storage.UPLOAD_ROOT)):
        assert ROOT not in Path(path).resolve().parents, path


def test_preflight_accepte_un_chemin_linux_depuis_windows():
    import importlib.util
    spec = importlib.util.spec_from_file_location('cp', ROOT / 'scripts' / 'cloud_preflight.py')
    cp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cp)
    assert cp.check_upload_dir({'PROFITOS_UPLOAD_DIR': '/var/data/uploads'}) == []
    # chemin relatif refusé (selon le dossier courant, l'alerte « /tmp » peut s'y ajouter)
    assert 'PROFITOS_UPLOAD_DIR doit être un chemin absolu.' in cp.check_upload_dir({'PROFITOS_UPLOAD_DIR': 'uploads'})


def test_un_seul_fichier_de_log_par_processus(monkeypatch):
    """v492.1 — create_app répété (tests) n'ouvre qu'une fois logs/profitos.log : sous Windows,
    des centaines de gestionnaires sur le même fichier faisaient échouer la rotation."""
    import logging
    from logging.handlers import RotatingFileHandler
    from profitos import create_app
    from profitos.config import DevelopmentConfig
    monkeypatch.setenv('PROFITOS_FILE_LOG', '1')
    logger = logging.getLogger('profitos')
    before = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
    try:
        create_app(DevelopmentConfig)
        create_app(DevelopmentConfig)
        create_app(DevelopmentConfig)
        files = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(files) - len(before) <= 1
        assert len({h.baseFilename for h in files}) == len(files)
    finally:
        for h in logger.handlers[:]:
            if isinstance(h, RotatingFileHandler) and h not in before:
                logger.removeHandler(h)
                h.close()


def test_pas_de_fichier_de_log_pendant_les_tests():
    import os
    assert os.environ.get('PROFITOS_FILE_LOG') == '0'


def test_go_live_indique_la_vraie_route_du_webhook_stripe():
    """v493.1 — le contrôle de mise en vente renvoyait vers /webhooks/stripe, une route qui
    n'existe pas : Stripe aurait reçu une 404 et les abonnements payés ne s'activaient pas."""
    src = (ROOT / 'scripts' / 'go_live_check.py').read_text(encoding='utf-8')
    assert '/webhooks/stripe' not in src and '/billing/webhook' in src
    routes = (ROOT / 'profitos' / 'routes' / 'account.py').read_text(encoding='utf-8')
    assert "@app.route('/billing/webhook',methods=['POST'])" in routes


def test_retour_powens_suit_le_domaine_public(monkeypatch):
    """v493.3 — en production, l'adresse de retour Powens suit APP_BASE_URL (elle était figée
    sur app.profitos.fr, l'ancien domaine)."""
    from profitos.routes import bank_sync
    monkeypatch.delenv('POWENS_REDIRECT_URI', raising=False)
    monkeypatch.setenv('PROFITOS_ENV', 'production')
    monkeypatch.setenv('APP_BASE_URL', 'https://veqalio.com/')
    assert bank_sync._callback_url() == 'https://veqalio.com/banking/callback'
    monkeypatch.setenv('POWENS_REDIRECT_URI', 'https://autre.exemple/retour')
    assert bank_sync._callback_url() == 'https://autre.exemple/retour'

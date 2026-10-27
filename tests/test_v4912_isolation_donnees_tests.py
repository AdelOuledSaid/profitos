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

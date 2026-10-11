"""Stockage des documents clients (factures d'achat, justificatifs, documents
cabinet, imports).

Production : disque persistant Render monté sur /var/data, avec
PROFITOS_UPLOAD_DIR=/var/data/uploads (vérifié par scripts/cloud_preflight.py).
Développement : <repo>/uploads.

Ce module centralise la résolution des chemins pour pouvoir brancher plus tard
un stockage objet (S3 compatible) sans réécrire les routes : les routes
existantes continuent d'utiliser runtime.UP / save_upload(), qui s'appuient
sur ce module. Un backend S3 implémenterait les mêmes fonctions
(write_bytes / read_bytes / delete) derrière `storage_backend()`.
"""
import os
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
UPLOAD_ROOT = Path(os.environ.get('PROFITOS_UPLOAD_DIR') or (BASE / 'uploads'))

# Disposition historique conservée (aucun fichier existant n'est déplacé) :
#   <racine>/<catégorie>/<org_id>/<fichier>   pour les catégories ci-dessous
#   <racine>/org_<org_id>/<catégorie>/<fichier> pour save_upload() (imports)
CATEGORIES = {
    'purchase_documents',   # factures fournisseurs (upload, boîte mail, WeInvoice, cloud)
    'expense_receipts',     # justificatifs de notes de frais
    'cabinet_documents',    # documents du portail cabinet
}

_SAFE_PART = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$')


def storage_backend():
    """'local' aujourd'hui ; 's3' le jour où un backend objet est ajouté."""
    return (os.environ.get('PROFITOS_STORAGE_BACKEND') or 'local').strip().lower()


def storage_root():
    return UPLOAD_ROOT


def upload_dir_writable():
    """(True, None) si le dossier des documents existe ou peut être créé et accepte une
    écriture ; sinon (False, raison). Utilisé par /readyz en production."""
    import uuid
    try:
        UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        probe = UPLOAD_ROOT / f'.ready-{uuid.uuid4().hex}'
        probe.write_bytes(b'ok')
        probe.unlink()
        return True, None
    except OSError as exc:
        return False, f'{type(exc).__name__}: {exc}'


def _check_part(value, label):
    value = str(value)
    if not _SAFE_PART.match(value) or '..' in value:
        raise ValueError(f'{label} de stockage invalide.')
    return value


def storage_dir(category, org_id, create=True):
    """Répertoire d'une catégorie de documents pour une organisation."""
    category = _check_part(category, 'Catégorie')
    root = UPLOAD_ROOT / category / str(int(org_id))
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def storage_path(category, org_id, name, create_dir=True):
    """Chemin d'un document. `name` est un nom de fichier simple (pas de /)."""
    name = _check_part(name, 'Nom de fichier')
    return storage_dir(category, org_id, create=create_dir) / name


def upload_dir(org_id, category, create=True):
    """Disposition historique de save_upload() : <racine>/org_<id>/<catégorie>."""
    category = _check_part(category, 'Catégorie')
    root = UPLOAD_ROOT / f'org_{int(org_id)}' / category
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def write_bytes(category, org_id, name, data):
    path = storage_path(category, org_id, name)
    tmp = path.with_name(path.name + '.part')
    tmp.write_bytes(data)
    os.replace(tmp, path)  # écriture atomique : jamais de fichier tronqué
    return path


def read_bytes(category, org_id, name):
    return storage_path(category, org_id, name, create_dir=False).read_bytes()


def delete(category, org_id, name):
    try:
        storage_path(category, org_id, name, create_dir=False).unlink(missing_ok=True)
    except ValueError:
        pass


def org_directories(org_id):
    """Tous les répertoires de documents d'une organisation (purge RGPD, export)."""
    org_id = int(org_id)
    dirs = [UPLOAD_ROOT / f'org_{org_id}']
    dirs += [UPLOAD_ROOT / cat / str(org_id) for cat in sorted(CATEGORIES)]
    return [d for d in dirs if d.exists()]

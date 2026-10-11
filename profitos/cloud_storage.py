"""Import automatique de factures fournisseurs depuis Dropbox ou Google Drive.

AVERTISSEMENT IMPORTANT : ce module est construit à partir de la
documentation OAuth2/API publique de Dropbox et Google telle que je la
connais, mais je n'ai aucun accès réseau dans cet environnement pour la
vérifier contre les vrais serveurs de ces fournisseurs. Les URLs, noms de
paramètres et formats de réponse ci-dessous sont corrects à ma connaissance
et ces API sont stables depuis des années, mais ce module doit être testé
avec de vraies applications OAuth enregistrées côté Dropbox/Google avant
tout usage réel — ce que je ne peux pas faire moi-même ici.

Configuration requise (variables d'environnement) :
  DROPBOX_CLIENT_ID, DROPBOX_CLIENT_SECRET
  GOOGLE_DRIVE_CLIENT_ID, GOOGLE_DRIVE_CLIENT_SECRET
  APP_BASE_URL (déjà utilisé ailleurs dans ProfitOS pour construire les
  URLs de redirection OAuth — ex. https://app.profitos.fr)

Chaque fournisseur nécessite une application OAuth enregistrée sur son
portail développeur (Dropbox App Console / Google Cloud Console), avec
comme URL de redirection autorisée :
  {APP_BASE_URL}/integrations/cloud-storage/dropbox/callback
  {APP_BASE_URL}/integrations/cloud-storage/google_drive/callback
"""
import os
from datetime import datetime, timedelta

import requests

PROVIDERS = {
    'dropbox': {
        'label': 'Dropbox',
        'authorize_url': 'https://www.dropbox.com/oauth2/authorize',
        'token_url': 'https://api.dropboxapi.com/oauth2/token',
        'scope': None,  # Dropbox gère les scopes via la config de l'app, pas via ce paramètre
    },
    'google_drive': {
        'label': 'Google Drive',
        'authorize_url': 'https://accounts.google.com/o/oauth2/v2/auth',
        'token_url': 'https://oauth2.googleapis.com/token',
        'scope': 'https://www.googleapis.com/auth/drive.readonly',
    },
}


def _client_credentials(provider):
    if provider == 'dropbox':
        return os.environ.get('DROPBOX_CLIENT_ID'), os.environ.get('DROPBOX_CLIENT_SECRET')
    if provider == 'google_drive':
        return os.environ.get('GOOGLE_DRIVE_CLIENT_ID'), os.environ.get('GOOGLE_DRIVE_CLIENT_SECRET')
    return None, None


def is_configured(provider):
    client_id, client_secret = _client_credentials(provider)
    return bool(client_id and client_secret)


def build_authorize_url(provider, redirect_uri, state):
    """Construit l'URL vers laquelle rediriger l'utilisateur pour démarrer
    le flux OAuth2. `state` doit être un jeton aléatoire imprévisible,
    stocké en session et revérifié au retour (protection CSRF du flux
    OAuth lui-même — indépendante de la protection CSRF des formulaires)."""
    cfg = PROVIDERS[provider]
    client_id, _ = _client_credentials(provider)
    params = {
        'client_id': client_id,
        'response_type': 'code',
        'redirect_uri': redirect_uri,
        'state': state,
    }
    if provider == 'dropbox':
        params['token_access_type'] = 'offline'  # nécessaire pour obtenir un refresh_token
    elif provider == 'google_drive':
        params['scope'] = cfg['scope']
        params['access_type'] = 'offline'
        params['prompt'] = 'consent'  # force le renvoi d'un refresh_token même en reconnexion
    query = '&'.join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
    return f"{cfg['authorize_url']}?{query}"


def exchange_code_for_token(provider, code, redirect_uri):
    """Échange le code d'autorisation contre un access_token (et, si
    disponible, un refresh_token). Lève ValueError avec un message clair
    en cas d'échec — ne masque jamais silencieusement une erreur OAuth."""
    client_id, client_secret = _client_credentials(provider)
    cfg = PROVIDERS[provider]
    data = {
        'code': code,
        'grant_type': 'authorization_code',
        'redirect_uri': redirect_uri,
        'client_id': client_id,
        'client_secret': client_secret,
    }
    try:
        resp = requests.post(cfg['token_url'], data=data, timeout=20)
    except requests.RequestException as e:
        raise ValueError(f"Connexion au serveur {cfg['label']} impossible : {e}") from e
    if resp.status_code != 200:
        raise ValueError(f"Échec de l'échange OAuth {cfg['label']} ({resp.status_code}) : {resp.text[:200]}")
    payload = resp.json()
    expires_in = payload.get('expires_in')
    expires_at = (datetime.utcnow() + timedelta(seconds=int(expires_in))).isoformat() if expires_in else None
    return {
        'access_token': payload.get('access_token'),
        'refresh_token': payload.get('refresh_token'),
        'expires_at': expires_at,
    }


def refresh_access_token(provider, refresh_token):
    """Renouvelle un access_token expiré à partir du refresh_token stocké.
    Renvoie None si le fournisseur n'a pas fourni de refresh_token à la
    connexion initiale (rare, mais géré) — l'appelant doit alors demander
    à l'utilisateur de se reconnecter."""
    if not refresh_token:
        return None
    client_id, client_secret = _client_credentials(provider)
    cfg = PROVIDERS[provider]
    data = {
        'refresh_token': refresh_token,
        'grant_type': 'refresh_token',
        'client_id': client_id,
        'client_secret': client_secret,
    }
    try:
        resp = requests.post(cfg['token_url'], data=data, timeout=20)
    except requests.RequestException as e:
        raise ValueError(f"Connexion au serveur {cfg['label']} impossible : {e}") from e
    if resp.status_code != 200:
        raise ValueError(f"Échec du renouvellement du jeton {cfg['label']} ({resp.status_code}).")
    payload = resp.json()
    expires_in = payload.get('expires_in')
    expires_at = (datetime.utcnow() + timedelta(seconds=int(expires_in))).isoformat() if expires_in else None
    return {'access_token': payload.get('access_token'), 'expires_at': expires_at}


def _ensure_valid_token(conn, connection):
    """Rafraîchit le jeton d'accès si sa date d'expiration est dépassée (ou
    proche), et met à jour la connexion en base. Retourne le jeton d'accès
    valide à utiliser pour l'appel API qui suit."""
    expires_at = connection['token_expires_at']
    if expires_at and datetime.fromisoformat(expires_at) > datetime.utcnow() + timedelta(minutes=2):
        return connection['access_token']
    refreshed = refresh_access_token(connection['provider'], connection['refresh_token'])
    if not refreshed:
        raise ValueError("Jeton expiré et aucun jeton de renouvellement disponible — reconnexion nécessaire.")
    conn.execute(
        'UPDATE cloud_storage_connections SET access_token=?,token_expires_at=? WHERE id=?',
        (refreshed['access_token'], refreshed['expires_at'], connection['id']),
    )
    conn.commit()
    return refreshed['access_token']


def list_folder_files(conn, connection):
    """Liste les fichiers PDF/image du dossier configuré pour cette
    connexion. Retourne une liste de dicts {file_id, name, mime_type}.
    Le format exact de la réponse diffère par fournisseur ; voir
    l'avertissement en tête de fichier sur la fiabilité non vérifiée."""
    token = _ensure_valid_token(conn, connection)
    headers = {'Authorization': f'Bearer {token}'}
    if connection['provider'] == 'dropbox':
        resp = requests.post(
            'https://api.dropboxapi.com/2/files/list_folder',
            headers={**headers, 'Content-Type': 'application/json'},
            json={'path': connection['folder_path'] or ''},
            timeout=20,
        )
        if resp.status_code != 200:
            raise ValueError(f"Dropbox : impossible de lister le dossier ({resp.status_code}).")
        entries = resp.json().get('entries', [])
        files = []
        for e in entries:
            if e.get('.tag') != 'file':
                continue
            name = e.get('name', '')
            if name.lower().endswith(('.pdf', '.jpg', '.jpeg', '.png')):
                files.append({'file_id': e.get('id'), 'name': name, 'mime_type': None})
        return files

    if connection['provider'] == 'google_drive':
        folder_id = connection['folder_path'] or 'root'
        query = f"'{folder_id}' in parents and trashed=false"
        resp = requests.get(
            'https://www.googleapis.com/drive/v3/files',
            headers=headers,
            params={'q': query, 'fields': 'files(id,name,mimeType)'},
            timeout=20,
        )
        if resp.status_code != 200:
            raise ValueError(f"Google Drive : impossible de lister le dossier ({resp.status_code}).")
        files = []
        for f in resp.json().get('files', []):
            mime = f.get('mimeType', '')
            if mime in ('application/pdf', 'image/jpeg', 'image/png'):
                files.append({'file_id': f.get('id'), 'name': f.get('name'), 'mime_type': mime})
        return files

    raise ValueError(f"Fournisseur inconnu : {connection['provider']!r}.")


def download_file(conn, connection, file_id):
    """Télécharge le contenu brut d'un fichier. Retourne les octets."""
    token = _ensure_valid_token(conn, connection)
    if connection['provider'] == 'dropbox':
        import json as json_module
        resp = requests.post(
            'https://content.dropboxapi.com/2/files/download',
            headers={
                'Authorization': f'Bearer {token}',
                'Dropbox-API-Arg': json_module.dumps({'path': file_id}),
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise ValueError(f"Dropbox : téléchargement échoué ({resp.status_code}).")
        return resp.content

    if connection['provider'] == 'google_drive':
        resp = requests.get(
            f'https://www.googleapis.com/drive/v3/files/{file_id}',
            headers={'Authorization': f'Bearer {token}'},
            params={'alt': 'media'},
            timeout=30,
        )
        if resp.status_code != 200:
            raise ValueError(f"Google Drive : téléchargement échoué ({resp.status_code}).")
        return resp.content

    raise ValueError(f"Fournisseur inconnu : {connection['provider']!r}.")

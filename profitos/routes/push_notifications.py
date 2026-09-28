"""Notifications push mobile — enregistrement des jetons d'appareil.

Ce module couvre uniquement l'ENREGISTREMENT du jeton (côté serveur,
entièrement testable ici) — pas l'ENVOI réel des notifications, qui
nécessite des identifiants Firebase (Android) et un certificat APNs (iOS)
que je ne peux ni configurer ni tester dans cet environnement.

Le jeton est envoyé par l'app Capacitor une fois l'utilisateur connecté
(même session/cookie que le site web, puisque la WebView Capacitor charge
directement app.profitos.fr) — voir mobile/README.md pour le morceau
client (plugin @capacitor/push-notifications) à ajouter dans l'app.
"""
from profitos.runtime import *


def register(app):
    @app.route('/api/mobile/csrf-token')
    @login_required
    def mobile_csrf_token():
        """Fournit le jeton CSRF de la session en cours, à inclure dans
        l'en-tête X-CSRF-Token pour les appels POST suivants depuis l'app
        mobile — une requête GET n'est jamais soumise à la protection CSRF,
        donc sans risque d'exposer ce jeton via cette route précise."""
        return jsonify({'csrf_token': session.get('csrf_token')})

    @app.route('/api/mobile/register-push-token', methods=['POST'])
    @login_required
    def register_push_token():
        payload = request.get_json(silent=True) or {}
        token = (payload.get('token') or '').strip()
        platform = (payload.get('platform') or '').strip().lower()
        if not token or platform not in ('ios', 'android'):
            return jsonify({'error': 'invalid_payload', 'message': "token et platform ('ios' ou 'android') sont requis."}), 400

        ac = auth_cx()
        ac.execute(
            """INSERT INTO push_device_tokens(user_id,platform,token,created_at,last_seen_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(user_id,token) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
            (session['user_id'], platform, token, now(), now()),
        )
        ac.commit(); ac.close()
        log_activity('PUSH_TOKEN_REGISTERED', f"Jeton push enregistré ({platform})")
        return jsonify({'status': 'registered'}), 201

    @app.route('/api/mobile/unregister-push-token', methods=['POST'])
    @login_required
    def unregister_push_token():
        payload = request.get_json(silent=True) or {}
        token = (payload.get('token') or '').strip()
        if not token:
            return jsonify({'error': 'invalid_payload', 'message': 'token est requis.'}), 400
        ac = auth_cx()
        ac.execute('DELETE FROM push_device_tokens WHERE user_id=? AND token=?', (session['user_id'], token))
        ac.commit(); ac.close()
        return jsonify({'status': 'unregistered'})

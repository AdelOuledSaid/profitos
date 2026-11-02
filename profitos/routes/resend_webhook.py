"""Webhook Resend (v492) : statut de distribution des e-mails (remis, retardé, rejeté,
signalé comme spam). Route publique de serveur à serveur, sans session ni CSRF, protégée
par la signature Svix et le secret RESEND_WEBHOOK_SECRET."""
import json
import os

from profitos.runtime import *
from profitos import email_delivery as ED

MAX_BODY = 256 * 1024


def register(app):

    @app.route('/webhooks/resend', methods=['POST'])
    def resend_webhook():
        secret = os.environ.get('RESEND_WEBHOOK_SECRET', '').strip()
        if not secret:
            return ('webhook disabled', 503)
        if request.content_length and request.content_length > MAX_BODY:
            return ('payload too large', 413)
        body = request.get_data(cache=False)
        try:
            event_id = ED.verify_signature(secret, request.headers, body)
        except ED.SignatureError as exc:
            log_ops_event('RESEND_WEBHOOK_REJECTED', 'WARNING', detail=str(exc))
            return ('invalid signature', 400)
        try:
            event = json.loads(body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return ('invalid json', 400)
        try:
            result = ED.apply_event(event, event_id)
        except Exception:
            current_app.logger.exception('Webhook Resend non traité (%s)', event_id)
            return ('error', 500)   # Resend réessaiera
        if result in ('bounced', 'complained'):
            log_ops_event('EMAIL_UNDELIVERABLE', 'WARNING', detail=f"{(event.get('type') or '')} id={event_id}")
        return jsonify(received=True, result=result)

"""Raccordement de l'entreprise à SUPER PDP (v495) : départ vers le parcours hébergé
(vérification KYC/KYB et consentement), retour, actualisation et déconnexion."""
import re

from profitos.runtime import *
from profitos import einvoice_superpdp as ES
from profitos import superpdp as SP


def _siren(c):
    row = c.execute('SELECT siret FROM company WHERE id=1').fetchone()
    digits = re.sub(r'\D', '', (row['siret'] if row else '') or '')
    return digits[:9] if len(digits) >= 9 else None


def register(app):

    def _guard():
        if not can_access('settings'):
            flash("Seuls le propriétaire ou un administrateur peuvent relier la facture électronique.")
            return redirect(url_for('company'))
        if not SP.is_configured():
            flash("La facture électronique n'est pas encore disponible : contactez le support.")
            return redirect(url_for('company'))
        return None

    # GET autorisé en plus de POST : le départ vers SUPER PDP ne modifie rien de durable (il
    # prépare l'état PKCE en session puis redirige), et un lien échappe à la règle de sécurité
    # « form-action » du navigateur, qui bloquait l'envoi quand Veqalio répond sur plusieurs
    # domaines. C'est le schéma habituel des connexions OAuth.
    @app.route('/facture-electronique/superpdp/relier', methods=['GET', 'POST'])
    @login_required
    def superpdp_connect():
        blocked = _guard()
        if blocked:
            return blocked
        c = cx()
        try:
            siren = _siren(c)
        finally:
            c.close()
        # v497.1 : le SIREN ne fait que pré-remplir le formulaire de SUPER PDP. Il ne bloque plus
        # le raccordement : sans lui (ou avec un numéro de bac à sable), SUPER PDP le demande.
        user = current_user()
        try:
            target = ES.start_url(session, siren=siren, login_hint=(user['email'] if user else None))
        except SP.SuperPdpError as exc:
            flash(str(exc))
            return redirect(url_for('company'))
        log_activity('EINVOICE_CONNECT_STARTED', f"Raccordement SUPER PDP démarré (entreprise {SP.company_number() or siren or 'à choisir'})")
        return redirect(target)

    @app.route('/facture-electronique/superpdp/retour')
    @login_required
    def superpdp_callback():
        blocked = _guard()
        if blocked:
            return blocked
        if request.args.get('error'):
            session.pop('superpdp_oauth', None)
            flash("Raccordement interrompu chez SUPER PDP : " + (request.args.get('error_description') or request.args['error'])[:200])
            return redirect(url_for('company'))
        from profitos.entities import current_entity_id
        org = current_org()
        c = cx()
        try:
            row = ES.finish(c, current_entity_id(), org['id'], session, request.args.get('state'), request.args.get('code'))
        except SP.SuperPdpError as exc:
            flash(str(exc))
            return redirect(url_for('company'))
        finally:
            c.close()
        log_activity('EINVOICE_CONNECTED', 'Entreprise reliée à SUPER PDP')
        if row and row['verified']:
            flash("Facture électronique reliée et vérifiée : vos factures peuvent être transmises.")
        else:
            flash("Entreprise reliée. SUPER PDP termine la vérification de l'entreprise : actualisez le statut dans "
                  "quelques minutes.")
        return redirect(url_for('company'))

    @app.route('/facture-electronique/superpdp/actualiser', methods=['POST'])
    @login_required
    def superpdp_refresh():
        blocked = _guard()
        if blocked:
            return blocked
        from profitos.entities import current_entity_id
        c = cx()
        try:
            verified = ES.refresh_status(c, current_entity_id(), current_org()['id'])
        except SP.SuperPdpError as exc:
            flash(str(exc))
        else:
            flash("✅ Entreprise vérifiée par SUPER PDP." if verified else "🟡 Vérification encore en cours chez SUPER PDP.")
        finally:
            c.close()
        return redirect(url_for('company'))

    @app.route('/facture-electronique/superpdp/deconnecter', methods=['POST'])
    @login_required
    def superpdp_disconnect():
        if not can_access('settings'):
            abort(403)
        from profitos.entities import current_entity_id
        c = cx()
        try:
            ES.disconnect(c, current_entity_id())
        finally:
            c.close()
        log_activity('EINVOICE_DISCONNECTED', 'Facture électronique déconnectée (SUPER PDP)')
        flash("Facture électronique déconnectée. Les factures déjà transmises restent chez SUPER PDP.")
        return redirect(url_for('company'))

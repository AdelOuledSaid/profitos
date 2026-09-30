from profitos.runtime import *
from profitos.swan_baas import (
    is_configured, current_environment, get_server_token, list_accounts,
    request_new_account, request_card, write_operations_enabled, graphql_query, company_registry_data_fr,
)


def _swan_webhook_secret_ok(req):
    import os, secrets
    expected = (os.environ.get('SWAN_WEBHOOK_SECRET') or '').strip()
    supplied = (req.headers.get('x-swan-secret') or '').strip()
    return bool(expected and len(expected) >= 32 and supplied and secrets.compare_digest(expected, supplied))


def register(app):
    @app.route('/settings/swan')
    @login_required
    @require_area('settings')
    def swan_settings():
        from profitos.entities import current_entity_id, accessible_entities
        c = cx()
        eid = current_entity_id()
        ef = 'entity_id=?' if eid else 'entity_id IS NULL'
        ep = (eid,) if eid else ()
        local_accounts = c.execute(
            f'SELECT * FROM swan_accounts WHERE {ef} ORDER BY requested_at DESC', ep
        ).fetchall()
        cards_by_account = {}
        for a in local_accounts:
            cards_by_account[a['id']] = c.execute(
                'SELECT * FROM swan_cards WHERE swan_account_row_id=? ORDER BY requested_at DESC', (a['id'],)
            ).fetchall()
        entities = accessible_entities(c, session.get('user_id'))
        c.close()

        remote_error = None
        remote_accounts = []
        if is_configured():
            try:
                token = get_server_token()
                all_remote_accounts = list_accounts(token)
                allowed_remote_ids = {str(a['swan_account_id']) for a in local_accounts}
                remote_accounts = [a for a in all_remote_accounts if str(a.get('id') or a.get('account_id') or '') in allowed_remote_ids]
            except ValueError as e:
                remote_error = str(e)

        return render_template(
            'swan_settings.html', configured=is_configured(), environment=current_environment(),
            local_accounts=local_accounts, cards_by_account=cards_by_account, entities=entities,
            current_entity_id=current_entity_id(), remote_accounts=remote_accounts, remote_error=remote_error,
            write_operations_enabled=write_operations_enabled(), registry_preview=session.pop('swan_registry_preview', None),
        )

    @app.route('/settings/swan/company-registry', methods=['POST'])
    @login_required
    @require_area('settings')
    def swan_company_registry():
        siren = (request.form.get('siren') or '').strip()
        try:
            info = company_registry_data_fr(siren)
            session['swan_registry_preview'] = info
            log_activity('SWAN_RNE_LOOKUP_OK', 'Préremplissage RNE Swan réussi')
            flash("Entreprise trouvée dans le registre RNE via Swan. Vérifiez les informations avant l'onboarding.")
        except ValueError as e:
            session.pop('swan_registry_preview', None)
            log_activity('SWAN_RNE_LOOKUP_FAILED', f"Échec RNE Swan : {str(e)[:180]}")
            flash(f"Recherche RNE impossible : {e}")
        return redirect(url_for('swan_settings'))

    @app.route('/settings/swan/test-connection', methods=['POST'])
    @login_required
    @require_area('settings')
    def swan_test_connection():
        try:
            token = get_server_token()
            # Read-only GraphQL call: no account/card/payment mutation.
            data = graphql_query(token, 'query ProfitOSConnectionTest { __typename }')
            if not data:
                raise ValueError("Swan a répondu sans données GraphQL.")
            log_activity('SWAN_CONNECTION_TEST_OK', f"Connexion Swan {current_environment()} validée")
            flash(f"Connexion Swan {current_environment()} réussie : OAuth + GraphQL sont opérationnels.")
        except ValueError as e:
            log_activity('SWAN_CONNECTION_TEST_FAILED', f"Échec connexion Swan : {str(e)[:180]}")
            flash(f"Connexion Swan impossible : {e}")
        return redirect(url_for('swan_settings'))

    @app.route('/settings/swan/compte/nouveau', methods=['POST'])
    @login_required
    @require_area('settings')
    def swan_account_request():
        if not write_operations_enabled():
            flash("Demandes de compte/carte Swan désactivées tant que le flux n'a pas été validé en Sandbox.")
            return redirect(url_for('swan_settings'))
        if not is_configured():
            flash("Swan n'est pas configuré côté serveur (SWAN_CLIENT_ID / SWAN_CLIENT_SECRET manquants).")
            return redirect(url_for('swan_settings'))
        name = (request.form.get('name') or '').strip()
        if not name:
            flash("Le nom du compte est obligatoire.")
            return redirect(url_for('swan_settings'))
        entity_id_raw = request.form.get('entity_id')
        entity_id = int(entity_id_raw) if entity_id_raw and entity_id_raw.isdigit() else None
        c = cx()
        from profitos.entities import user_can_access_entity
        if not user_can_access_entity(c, session.get('user_id'), entity_id):
            c.close(); abort(403)
        c.close()

        try:
            token = get_server_token()
            result = request_new_account(token, name)
        except ValueError as e:
            flash(f"Demande de compte refusée : {e}")
            return redirect(url_for('swan_settings'))

        if not result.get('account_id'):
            flash("Swan n'a renvoyé aucun identifiant de compte — la demande n'a probablement pas abouti.")
            return redirect(url_for('swan_settings'))

        c = cx()
        c.execute(
            """INSERT INTO swan_accounts(entity_id,swan_account_id,name,status,consent_url,requested_at)
               VALUES(?,?,?,?,?,?)""",
            (entity_id, result['account_id'], name, result.get('status') or 'pending',
             result.get('consent_url'), now()),
        )
        c.commit(); c.close()
        log_activity('SWAN_ACCOUNT_REQUESTED', f"Demande de compte Swan « {name} »")
        if result.get('consent_url'):
            flash("Compte demandé — il ne sera actif qu'après validation sur la page Swan (lien ci-dessous).")
        else:
            flash("Compte demandé, mais Swan n'a renvoyé aucun lien de validation — vérifie manuellement sur ton tableau de bord Swan.")
        return redirect(url_for('swan_settings'))

    @app.route('/settings/swan/compte/<int:account_row_id>/carte', methods=['POST'])
    @login_required
    @require_area('settings')
    def swan_card_request(account_row_id):
        if not write_operations_enabled():
            flash("Demandes de compte/carte Swan désactivées tant que le flux n'a pas été validé en Sandbox.")
            return redirect(url_for('swan_settings'))
        if not is_configured():
            flash("Swan n'est pas configuré côté serveur.")
            return redirect(url_for('swan_settings'))
        c = cx()
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        account = c.execute('SELECT * FROM swan_accounts WHERE id=? AND entity_id IS ?', (account_row_id, eid)).fetchone()
        if not account:
            c.close(); abort(404)
        holder_name = (request.form.get('holder_name') or '').strip()
        if not holder_name:
            c.close()
            flash("Le nom du porteur de carte est obligatoire.")
            return redirect(url_for('swan_settings'))

        try:
            token = get_server_token()
            result = request_card(token, account['swan_account_id'], holder_name)
        except ValueError as e:
            c.close()
            flash(f"Demande de carte refusée : {e}")
            return redirect(url_for('swan_settings'))

        c.execute(
            """INSERT INTO swan_cards(swan_account_row_id,swan_card_id,holder_name,status,consent_url,requested_at)
               VALUES(?,?,?,?,?,?)""",
            (account_row_id, result.get('card_id'), holder_name, result.get('status') or 'pending',
             result.get('consent_url'), now()),
        )
        c.commit(); c.close()
        log_activity('SWAN_CARD_REQUESTED', f"Demande de carte Swan pour {holder_name}")
        if result.get('consent_url'):
            flash("Carte demandée — elle ne sera active qu'après validation sur la page Swan (lien ci-dessous).")
        else:
            flash("Carte demandée, mais Swan n'a renvoyé aucun lien de validation — vérifie manuellement sur ton tableau de bord Swan.")
        return redirect(url_for('swan_settings'))

    @app.route('/webhooks/swan', methods=['POST'])
    def swan_webhook():
        if not _swan_webhook_secret_ok(request):
            abort(401)
        payload = request.get_json(silent=True) or {}
        event_id = str(payload.get('eventId') or '').strip()
        event_type = str(payload.get('eventType') or '').strip()
        resource_id = str(payload.get('resourceId') or '').strip()
        if not event_id or not event_type or not resource_id:
            return jsonify({'ok': False, 'error': 'invalid_event'}), 400
        c = cx()
        if c.execute('SELECT id FROM swan_webhook_events WHERE event_id=?', (event_id,)).fetchone():
            c.close()
            return jsonify({'ok': True, 'duplicate': True}), 200
        c.execute(
            """INSERT INTO swan_webhook_events(event_id,event_type,resource_id,project_id,event_date,processed_at)
               VALUES(?,?,?,?,?,?)""",
            (event_id, event_type, resource_id, str(payload.get('projectId') or ''),
             str(payload.get('eventDate') or ''), now()),
        )
        # Notification minimale : les données sensibles sont relues via l'API Swan.
        c.commit(); c.close()
        return jsonify({'ok': True}), 200


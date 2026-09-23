from profitos.runtime import *
from profitos.swan_baas import (
    is_configured, current_environment, get_server_token, list_accounts,
    request_new_account, request_card,
)


def register(app):
    @app.route('/settings/swan')
    @login_required
    @require_area('settings')
    def swan_settings():
        from profitos.entities import current_entity_id, list_all_entities
        c = cx()
        local_accounts = c.execute(
            'SELECT * FROM swan_accounts ORDER BY requested_at DESC'
        ).fetchall()
        cards_by_account = {}
        for a in local_accounts:
            cards_by_account[a['id']] = c.execute(
                'SELECT * FROM swan_cards WHERE swan_account_row_id=? ORDER BY requested_at DESC', (a['id'],)
            ).fetchall()
        entities = list_all_entities(c)
        c.close()

        remote_error = None
        remote_accounts = []
        if is_configured():
            try:
                token = get_server_token()
                remote_accounts = list_accounts(token)
            except ValueError as e:
                remote_error = str(e)

        return render_template(
            'swan_settings.html', configured=is_configured(), environment=current_environment(),
            local_accounts=local_accounts, cards_by_account=cards_by_account, entities=entities,
            current_entity_id=current_entity_id(), remote_accounts=remote_accounts, remote_error=remote_error,
        )

    @app.route('/settings/swan/compte/nouveau', methods=['POST'])
    @login_required
    @require_area('settings')
    def swan_account_request():
        if not is_configured():
            flash("Swan n'est pas configuré côté serveur (SWAN_CLIENT_ID / SWAN_CLIENT_SECRET manquants).")
            return redirect(url_for('swan_settings'))
        name = (request.form.get('name') or '').strip()
        if not name:
            flash("Le nom du compte est obligatoire.")
            return redirect(url_for('swan_settings'))
        entity_id_raw = request.form.get('entity_id')
        entity_id = int(entity_id_raw) if entity_id_raw and entity_id_raw.isdigit() else None

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
        if not is_configured():
            flash("Swan n'est pas configuré côté serveur.")
            return redirect(url_for('swan_settings'))
        c = cx()
        account = c.execute('SELECT * FROM swan_accounts WHERE id=?', (account_row_id,)).fetchone()
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

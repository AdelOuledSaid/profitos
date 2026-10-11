from profitos.runtime import *
from profitos.swan_baas import (
    is_configured, current_environment, get_server_token, list_accounts,
    request_new_account, request_card, write_operations_enabled, graphql_query,
    feature_enabled, bind_account, bind_onboarding, bound_account_ids, organization_for_resource, sync_onboarding_accounts, company_registry_data_fr, company_onboarding_v2_preflight, create_company_onboarding_v2_sandbox, create_individual_onboarding_v2_sandbox, build_user_authorization_url, exchange_user_authorization_code, current_swan_user_id, initiate_sepa_credit_transfer_sandbox,
)


def _swan_webhook_secret_ok(req):
    import os, secrets
    expected = (os.environ.get('SWAN_WEBHOOK_SECRET') or '').strip()
    supplied = (req.headers.get('x-swan-secret') or '').strip()
    return bool(expected and len(expected) >= 32 and supplied and secrets.compare_digest(expected, supplied))


def register(app):
    from profitos.swan_baas import feature_enabled as swan_feature_enabled
    app.jinja_env.globals['swan_enabled'] = swan_feature_enabled

    @app.before_request
    def swan_feature_gate():
        # « Compte Pro » masqué et inaccessible tant que Swan n'est pas configuré ET
        # explicitement activé (SWAN_ENABLED=1). Le webhook reste protégé par son secret.
        endpoint = request.endpoint or ''
        if endpoint.startswith('swan_') and endpoint != 'swan_webhook' and not feature_enabled():
            abort(404)
    @app.route('/settings/swan')
    @login_required
    @require_area('settings')
    def swan_settings():
        from profitos.entities import current_entity_id, accessible_entities, current_entity
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
        company = current_entity(c)
        c.close()

        remote_error = None
        remote_accounts = []
        if is_configured():
            try:
                token = get_server_token()
                sync_onboarding_accounts(token, session['org_id'])
                all_remote_accounts = list_accounts(token)
                # Le projet Swan contient les comptes de toutes les organisations (Sandbox
                # comme Live) : seuls les comptes RATTACHÉS à cette organisation sont visibles.
                allowed_remote_ids = bound_account_ids(session['org_id'])
                remote_accounts = [a for a in all_remote_accounts if str(a.get('id') or a.get('account_id') or '') in allowed_remote_ids]
            except ValueError as e:
                remote_error = str(e)

        return render_template(
            'swan_settings.html', configured=is_configured(), environment=current_environment(),
            local_accounts=local_accounts, cards_by_account=cards_by_account, entities=entities,
            current_entity_id=current_entity_id(), company=company, remote_accounts=remote_accounts, remote_error=remote_error,
            write_operations_enabled=write_operations_enabled(), swan_user_connected=bool(session.get('swan_user_id')), swan_oauth_open_url=session.get('swan_oauth_open_url'), swan_transfer_consent_url=session.get('swan_transfer_consent_url'), swan_transfer_payment_id=session.get('swan_transfer_payment_id'), registry_preview=session.get('swan_registry_preview'), onboarding_result=session.pop('swan_onboarding_result', None), individual_onboarding_result=session.pop('swan_individual_onboarding_result', None),
        )

    @app.route('/settings/swan/onboarding-v2/preflight', methods=['POST'])
    @login_required
    @require_area('settings')
    def swan_onboarding_v2_preflight():
        try:
            result = company_onboarding_v2_preflight()
            if result.get('ok'):
                flash("Onboarding Swan v2 compatible : mutation et champs requis détectés dans l'environnement de test.")
                log_activity('SWAN_ONBOARDING_V2_PREFLIGHT_OK', 'Schéma onboarding Swan v2 compatible')
            else:
                missing = ', '.join(result.get('missing') or [])
                flash(f"Onboarding Swan v2 incomplet dans cet environnement de test. Éléments absents : {missing}")
                log_activity('SWAN_ONBOARDING_V2_PREFLIGHT_FAILED', f"Champs absents : {missing[:180]}")
        except ValueError as e:
            flash(f"Pré-vérification onboarding Swan impossible : {e}")
            log_activity('SWAN_ONBOARDING_V2_PREFLIGHT_ERROR', str(e)[:180])
        return redirect(url_for('swan_settings'))

    @app.route('/settings/swan/company-registry', methods=['POST'])
    @login_required
    @require_area('settings')
    def swan_company_registry():
        siren = (request.form.get('siren') or '').strip()
        try:
            info = company_registry_data_fr(siren)
            info['registrationNumber'] = siren
            session['swan_registry_preview'] = info
            log_activity('SWAN_RNE_LOOKUP_OK', 'Préremplissage RNE Swan réussi')
            flash("Entreprise trouvée dans le registre RNE via Swan. Vérifiez les informations avant l'onboarding.")
        except ValueError as e:
            session.pop('swan_registry_preview', None)
            log_activity('SWAN_RNE_LOOKUP_FAILED', f"Échec RNE Swan : {str(e)[:180]}")
            flash(f"Recherche RNE impossible : {e}")
        return redirect(url_for('swan_settings'))

    @app.route('/settings/swan/onboarding-v2/create', methods=['POST'])
    @login_required
    @require_area('settings')
    def swan_onboarding_v2_create():
        if current_environment() != 'sandbox':
            abort(403)
        if request.form.get('confirm_sandbox') != 'yes':
            flash("Confirmez explicitement la création de l'onboarding de test.")
            return redirect(url_for('swan_settings'))

        preview = session.get('swan_registry_preview') or {}
        address = preview.get('address') or {}
        siren = ''.join(ch for ch in str(preview.get('registrationNumber') or '') if ch.isdigit())
        if len(siren) != 9:
            flash("SIREN RNE validé absent. Relancez d'abord la recherche RNE.")
            return redirect(url_for('swan_settings'))

        required = [
            'email','business_activity','business_activity_description','monthly_payment_volume',
            'regulatory_classification','first_name','last_name','sex','birth_date','birth_city',
            'birth_postal_code','nationality','person_address_line1','person_city','person_postal_code'
        ]
        missing = [k for k in required if not (request.form.get(k) or '').strip()]
        if missing:
            flash("Onboarding incomplet : " + ", ".join(missing))
            return redirect(url_for('swan_settings'))

        try:
            ownership = int(request.form.get('ownership_percentage') or '100')
        except ValueError:
            ownership = 0
        if ownership < 1 or ownership > 100:
            flash("Le pourcentage de détention doit être compris entre 1 et 100.")
            return redirect(url_for('swan_settings'))

        input_data = {
            'accountInfo': {'country': 'FRA'},
            'accountAdmin': {
                'email': request.form['email'].strip(),
                'preferredLanguage': 'fr',
                'typeOfRepresentation': 'LegalRepresentative',
            },
            'company': {
                'name': (request.form.get('company_name') or preview.get('name') or '').strip(),
                'registrationNumber': siren,
                'legalFormCode': (request.form.get('legal_form_code') or preview.get('legalFormCode') or preview.get('legalForm') or '').strip(),
                'businessActivity': request.form['business_activity'].strip(),
                'businessActivityDescription': request.form['business_activity_description'].strip(),
                'monthlyPaymentVolume': request.form['monthly_payment_volume'].strip(),
                'regulatoryClassification': request.form['regulatory_classification'].strip(),
                'address': {
                    'addressLine1': (request.form.get('company_address_line1') or address.get('addressLine1') or '').strip(),
                    'city': (request.form.get('company_city') or address.get('city') or '').strip(),
                    'postalCode': (request.form.get('company_postal_code') or address.get('postalCode') or '').strip(),
                    'country': 'FRA',
                },
                'relatedIndividuals': [{
                    'type': 'LegalRepresentativeAndUltimateBeneficialOwner',
                    'firstName': request.form['first_name'].strip(),
                    'lastName': request.form['last_name'].strip(),
                    'sex': request.form['sex'].strip(),
                    'birthInfo': {
                        'birthDate': request.form['birth_date'].strip(),
                        'country': 'FRA',
                        'city': request.form['birth_city'].strip(),
                        'postalCode': request.form['birth_postal_code'].strip(),
                    },
                    'address': {
                        'addressLine1': request.form['person_address_line1'].strip(),
                        'city': request.form['person_city'].strip(),
                        'country': 'FRA',
                        'postalCode': request.form['person_postal_code'].strip(),
                    },
                    'nationality': request.form['nationality'].strip().upper(),
                    'unitedStatesTaxInfo': {'isUnitedStatesPerson': False},
                    'legalRepresentative': {'roles': (request.form.get('representative_role') or 'Dirigeant').strip()},
                    'ultimateBeneficialOwner': {
                        'qualificationType': 'Ownership',
                        'ownership': {'type': 'Direct', 'totalPercentage': ownership},
                    },
                }],
            },
        }
        if not all([
            input_data['company']['name'], input_data['company']['legalFormCode'],
            input_data['company']['address']['addressLine1'], input_data['company']['address']['city'],
            input_data['company']['address']['postalCode']
        ]):
            flash("Les données RNE nécessaires sont incomplètes. Relancez d'abord la recherche RNE.")
            return redirect(url_for('swan_settings'))

        try:
            result = create_company_onboarding_v2_sandbox(input_data)
            from profitos.entities import current_entity_id
            bind_onboarding(session['org_id'], result.get('id'), current_entity_id())
            session['swan_onboarding_result'] = result
            log_activity('SWAN_ONBOARDING_V2_CREATED_SANDBOX', f"Onboarding Swan Sandbox {result.get('id')}")
            flash("Onboarding Swan v2 créé dans l'environnement de test. Aucune activation Live n'a été effectuée.")
        except ValueError as e:
            log_activity('SWAN_ONBOARDING_V2_CREATE_FAILED', str(e)[:180])
            flash(f"Création onboarding Swan (environnement de test) impossible : {e}")
        return redirect(url_for('swan_settings'))


    @app.route('/settings/swan/individual-onboarding-v2/create', methods=['POST'])
    @login_required
    @require_area('settings')
    def swan_individual_onboarding_v2_create():
        if current_environment() != 'sandbox':
            flash("Création d'onboarding individuel bloquée hors environnement de test.")
            return redirect(url_for('swan_settings'))
        if request.form.get('confirm_individual_sandbox') != 'yes':
            flash("Confirmez explicitement la création de l'onboarding individuel de test.")
            return redirect(url_for('swan_settings'))

        email = (request.form.get('individual_email') or '').strip()
        address_line1 = (request.form.get('individual_address_line1') or '').strip()
        city = (request.form.get('individual_city') or '').strip()
        postal_code = (request.form.get('individual_postal_code') or '').strip()
        employment_status = (request.form.get('employment_status') or '').strip()
        monthly_income = (request.form.get('monthly_income') or '').strip()

        if not email or '@' not in email or not address_line1 or not city or not postal_code:
            flash("Complétez l'e-mail et l'adresse de l'utilisateur de test.")
            return redirect(url_for('swan_settings'))
        allowed_employment = {'Employee', 'SelfEmployed', 'Student', 'Retired', 'Unemployed'}
        allowed_income = {'LessThan500', 'Between500And1500', 'Between1500And3000', 'Between3000And4500', 'MoreThan4500'}
        if employment_status not in allowed_employment or monthly_income not in allowed_income:
            flash("Valeurs emploi/revenu non autorisées.")
            return redirect(url_for('swan_settings'))

        input_data = {
            'accountInfo': {'country': 'FRA'},
            'accountAdmin': {
                'email': email,
                'employmentStatus': employment_status,
                'preferredLanguage': 'fr',
                'monthlyIncome': monthly_income,
                'address': {
                    'addressLine1': address_line1,
                    'city': city,
                    'country': 'FRA',
                    'postalCode': postal_code,
                },
                'unitedStatesTaxInfo': {'isUnitedStatesPerson': False},
            },
        }
        try:
            result = create_individual_onboarding_v2_sandbox(input_data)
            from profitos.entities import current_entity_id
            bind_onboarding(session['org_id'], result.get('id'), current_entity_id())
            session['swan_individual_onboarding_result'] = result
            log_activity('SWAN_INDIVIDUAL_ONBOARDING_V2_CREATED', f"Sandbox onboarding {result.get('id')}")
            flash("Onboarding individuel Swan (environnement de test) créé.")
        except ValueError as e:
            flash(f"Création onboarding individuel Swan (environnement de test) impossible : {e}")
            log_activity('SWAN_INDIVIDUAL_ONBOARDING_V2_ERROR', str(e)[:180])
        return redirect(url_for('swan_settings'))

    @app.route('/settings/swan/user/connect', methods=['POST'])
    @login_required
    @require_area('settings')
    def swan_user_connect():
        if current_environment() != 'sandbox':
            flash("Connexion utilisateur Swan bloquée hors environnement de test.")
            return redirect(url_for('swan_settings'))
        import secrets
        state = secrets.token_urlsafe(32)
        session['swan_oauth_state'] = state
        try:
            # Some installed/PWA browser contexts can swallow a cross-origin 302
            # after a POST. Generate the exact OAuth URL server-side, then expose
            # it as an explicit GET link on the settings page.
            session['swan_oauth_open_url'] = build_user_authorization_url(state)
            flash("Autorisation Swan prête. Cliquez sur « Ouvrir Swan (environnement de test) » ci-dessous.")
            return redirect(url_for('swan_settings'))
        except ValueError as e:
            session.pop('swan_oauth_open_url', None)
            flash(str(e))
            return redirect(url_for('swan_settings'))

    @app.route('/settings/swan/oauth/callback', methods=['GET'])
    @login_required
    @require_area('settings')
    def swan_oauth_callback():
        import secrets
        expected = session.pop('swan_oauth_state', '')
        session.pop('swan_oauth_open_url', None)
        supplied = (request.args.get('state') or '').strip()
        code = (request.args.get('code') or '').strip()
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            abort(400)
        if not code:
            flash("Swan n'a renvoyé aucun code d'autorisation.")
            return redirect(url_for('swan_settings'))
        try:
            payload = exchange_user_authorization_code(code)
            user_id = current_swan_user_id(payload['access_token'], environment='sandbox')
            # Ne jamais stocker le token utilisateur dans la session navigateur.
            # Seul l'identifiant Swan non secret est conservé ; les opérations
            # sensibles utilisent ensuite l'impersonation avec le project token.
            session['swan_user_id'] = user_id
            log_activity('SWAN_USER_CONNECTED_SANDBOX', f'Utilisateur Swan Sandbox lié {user_id}')
            flash("Utilisateur Swan (environnement de test) connecté à ProfitOS.")
        except ValueError as e:
            flash(f"Connexion utilisateur Swan impossible : {e}")
        return redirect(url_for('swan_settings'))

    @app.route('/settings/swan/transfer/sepa', methods=['POST'])
    @login_required
    @require_area('settings')
    def swan_sepa_transfer():
        if current_environment() != 'sandbox':
            flash("Virements Swan bloqués hors environnement de test.")
            return redirect(url_for('swan_settings'))
        user_id = session.get('swan_user_id')
        if not user_id:
            flash("Connectez d'abord l'utilisateur Swan (environnement de test) avant d'initier un virement.")
            return redirect(url_for('swan_settings'))
        account_id = (request.form.get('account_id') or '').strip()
        beneficiary_name = (request.form.get('beneficiary_name') or '').strip()
        beneficiary_iban = (request.form.get('beneficiary_iban') or '').strip()
        amount = (request.form.get('amount') or '').strip()
        if request.form.get('confirm_sandbox_transfer') != 'yes':
            flash("Confirmez explicitement le virement de test.")
            return redirect(url_for('swan_settings'))
        try:
            # Le compte débiteur doit être rattaché à CETTE organisation (jamais un compte
            # quelconque du projet Swan) et exister côté Swan.
            if account_id not in bound_account_ids(session['org_id']):
                log_security_event('SWAN_TRANSFER_DENIED', 'BLOCKED', target='unbound_account')
                abort(403)
            token = get_server_token()
            allowed = {str(a.get('id') or a.get('account_id') or '') for a in list_accounts(token, environment='sandbox')}
            if account_id not in allowed:
                abort(403)
            result = initiate_sepa_credit_transfer_sandbox(
                token, user_id, account_id, beneficiary_name, beneficiary_iban, amount,
                f"{app_base_url()}{url_for('swan_settings')}",
            )
            log_activity('SWAN_SEPA_TRANSFER_INITIATED_SANDBOX', f"Paiement Swan {result.get('payment_id')}")
            # Do not rely on a cross-origin 302 after POST: installed/PWA browser
            # contexts can swallow it. Expose Swan's exact consentUrl as a GET link.
            if result.get('consent_url'):
                session['swan_transfer_consent_url'] = result['consent_url']
                session['swan_transfer_payment_id'] = result.get('payment_id') or ''
                flash("Virement Swan créé et en attente de consentement. Cliquez sur « Ouvrir la validation Swan » ci-dessous.")
                return redirect(url_for('swan_settings'))
            session.pop('swan_transfer_consent_url', None)
            session.pop('swan_transfer_payment_id', None)
            flash(f"Virement Swan créé (statut : {result.get('status') or 'inconnu'}), mais Swan n'a renvoyé aucune URL de consentement.")
        except ValueError as e:
            flash(f"Virement Swan impossible : {e}")
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
            flash("Demandes de compte/carte Swan désactivées tant que le flux n'a pas été validé en environnement de test.")
            return redirect(url_for('swan_settings'))
        if not is_configured():
            flash("Le compte professionnel n'est pas disponible pour le moment. Contactez le support pour l'activer.")
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

        try:
            bind_account(session['org_id'], result['account_id'], entity_id)
        except ValueError as e:
            flash(str(e))
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
            flash("Compte demandé, mais Swan n'a renvoyé aucun lien de validation — vérifiez manuellement sur votre tableau de bord Swan.")
        return redirect(url_for('swan_settings'))

    @app.route('/settings/swan/compte/<int:account_row_id>/carte', methods=['POST'])
    @login_required
    @require_area('settings')
    def swan_card_request(account_row_id):
        if not write_operations_enabled():
            flash("Demandes de compte/carte Swan désactivées tant que le flux n'a pas été validé en environnement de test.")
            return redirect(url_for('swan_settings'))
        if not is_configured():
            flash("Le compte professionnel n'est pas disponible pour le moment.")
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
            flash("Carte demandée, mais Swan n'a renvoyé aucun lien de validation — vérifiez manuellement sur votre tableau de bord Swan.")
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
        # Pas de session ici : l'organisation est retrouvée par le rattachement du compte /
        # de l'onboarding, et la connexion tenant est ouverte explicitement.
        org_id = organization_for_resource(resource_id)
        ac = auth_cx()
        try:
            if ac.execute('SELECT id FROM swan_webhook_inbox WHERE event_id=?', (event_id,)).fetchone():
                return jsonify({'ok': True, 'duplicate': True}), 200
            ac.execute('INSERT INTO swan_webhook_inbox(event_id,event_type,resource_id,organization_id,received_at) VALUES(?,?,?,?,?)',
                       (event_id[:120], event_type[:120], resource_id[:120], org_id, now()))
            ac.commit()
        finally:
            ac.close()
        if org_id is None:
            # Ressource inconnue (autre projet / compte non rattaché) : accusé de réception
            # sans traitement, pour que Swan ne relivre pas indéfiniment.
            return jsonify({'ok': True, 'routed': False}), 200
        c = tenant_cx_direct(org_id)
        try:
            if c.execute('SELECT id FROM swan_webhook_events WHERE event_id=?', (event_id,)).fetchone():
                return jsonify({'ok': True, 'duplicate': True}), 200
            c.execute(
                """INSERT INTO swan_webhook_events(event_id,event_type,resource_id,project_id,event_date,processed_at)
                   VALUES(?,?,?,?,?,?)""",
                (event_id, event_type, resource_id, str(payload.get('projectId') or ''),
                 str(payload.get('eventDate') or ''), now()),
            )
            # Notification minimale : les données sensibles sont relues via l'API Swan.
            c.commit()
        finally:
            c.close()
        return jsonify({'ok': True}), 200


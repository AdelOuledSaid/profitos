from profitos.runtime import *
from profitos.runtime import _token_user
from profitos import account_security as acsec
from profitos.plan_limits import PLAN_LIMITS, FEATURE_LABELS, plan_limit, within_limit, feature_enabled
from profitos.plan_usage import quota_state



# Version des CGU/CGV acceptées à l'inscription (enregistrée avec la date d'acceptation).
TERMS_VERSION='2026-10'


def register(app):
    @app.route('/organizations/new',methods=['POST'])
    @login_required
    @require_area('settings')
    def org_new():
        name=request.form.get('org_name','').strip()
        if not name: flash("Nom d'organisation requis."); return redirect(url_for('settings'))

        active_org=current_org()
        # Formule Multi-sociétés : la nouvelle organisation est rattachée à
        # l'organisation payeuse et hérite de son plan/statut (pas d'essai séparé).
        payer=billing_org(active_org) if active_org else None
        active_plan=effective_plan(active_org) if active_org else 'TRIAL'
        attach_to=None
        if payer and (plan_limit(active_plan,'organizations') or 0)>1 and org_has_access(payer) and current_role()=='OWNER':
            attach_to=payer
        c=auth_cx()

        if attach_to:
            owned_count=c.execute(
                'SELECT COUNT(*) AS n FROM organizations WHERE id=? OR parent_org_id=?',
                (attach_to['id'],attach_to['id'])
            ).fetchone()['n']
        else:
            owned_count=c.execute(
                "SELECT COUNT(*) AS n FROM memberships WHERE user_id=? AND role='OWNER'",
                (session['user_id'],)
            ).fetchone()['n']

        if not within_limit(active_plan,'organizations',owned_count,1):
            c.close()
            limit=plan_limit(active_plan,'organizations')
            log_security_event('PLAN_LIMIT','BLOCKED',target='organizations')
            flash(
                f"Votre formule {active_plan} autorise au maximum {pluralize(limit, 'organisation')}. "
                "Passez à une formule supérieure pour en créer davantage."
            )
            return redirect(url_for('settings'))
        slug=re.sub(r'[^a-z0-9]+','-',norm(name)).strip('-') or 'company'; base=slug; i=1
        while c.execute('SELECT id FROM organizations WHERE slug=?',(slug,)).fetchone(): i+=1; slug=f'{base}-{i}'
        if attach_to:
            c.execute("INSERT INTO organizations(name,slug,plan,status,trial_ends_at,parent_org_id,created_at,updated_at) VALUES(?,?,?,?,NULL,?,?,?)",(name,slug,attach_to['plan'],attach_to['status'],attach_to['id'],now(),now()))
        else:
            trial=(datetime.now(timezone.utc)+timedelta(days=14)).isoformat()
            c.execute("INSERT INTO organizations(name,slug,plan,status,trial_ends_at,created_at,updated_at) VALUES(?,?,'TRIAL','ACTIVE',?,?,?)",(name,slug,trial,now(),now()))
        oid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
        c.execute("INSERT INTO memberships(user_id,organization_id,role,created_at) VALUES(?,?,'OWNER',?)",(session['user_id'],oid,now())); c.commit(); c.close()
        session['org_id']=oid; session['role']='OWNER'; session.pop('current_entity_id',None); init_tenant_db()
        tc=cx(); tc.execute("INSERT OR IGNORE INTO app_settings(id,onboarding_complete,currency,locale,notifications_enabled,created_at,updated_at) VALUES(1,0,'EUR','fr-FR',1,?,?)",(now(),now())); tc.commit(); tc.close()
        log_activity('ORG_CREATED',f'Organisation créée : {name}')
        flash(f'Organisation "{name}" créée et sélectionnée.')
        return redirect(url_for('onboarding'))

    @app.route('/organizations/switch/<int:org_id>',methods=['POST'])
    @login_required
    def org_switch(org_id):
        c=auth_cx(); m=c.execute('SELECT * FROM memberships WHERE user_id=? AND organization_id=?',(session['user_id'],org_id)).fetchone(); c.close()
        if not m:
            flash("Vous n'appartenez pas à cette organisation.")
            return redirect(request.referrer or url_for('home'))
        session['org_id']=org_id; session['role']=m['role']; session.pop('current_entity_id',None); init_tenant_db()
        log_activity('ORG_SWITCH','Changement d\'organisation active')
        return redirect(url_for('home'))

    @app.route('/signup',methods=['GET','POST'])
    @rate_limit(10,900)
    def signup():
        if session.get('user_id'): return redirect(url_for('home'))
        if request.method=='GET' and request.args.get('ref'):
            session['pending_ref_code']=request.args.get('ref','').strip()
        if request.method=='POST':
            name=request.form.get('full_name','').strip(); email=request.form.get('email','').strip().lower(); pw=request.form.get('password',''); company=request.form.get('company_name','').strip()
            # En cas d'erreur, la saisie (hors mot de passe) est conservée.
            session['signup_form']={'full_name':name[:120],'email':email[:254],'company_name':company[:200]}
            if not name or not email or not pw or not company: flash('Tous les champs sont obligatoires.'); return redirect(request.url)
            if request.form.get('accept_terms')!='1':
                flash("Veuillez accepter les conditions générales d'utilisation et de vente pour créer votre compte."); return redirect(request.url)
            if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email) or len(email)>254:
                flash('Adresse e-mail invalide.'); return redirect(request.url)
            err=password_error(pw,email)
            if err: flash(err); return redirect(request.url)
            c=auth_cx()
            existing=c.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
            if existing:
                c.close()
                # Message identique à une inscription réussie (pas d'énumération des comptes) ;
                # le titulaire réel de l'adresse est prévenu par e-mail.
                acsec.dummy_password_check(pw)
                if check_recipient_rate('signup_existing',email):
                    acsec.notify_user(existing,'Tentative de création de compte',
                        "Quelqu'un a essayé de créer un compte ProfitOS avec votre adresse e-mail, qui possède déjà un compte. "
                        "Si c'était vous, connectez-vous ou utilisez « Mot de passe oublié ». Sinon, vous pouvez ignorer ce message.",
                        cta_label='Me connecter',cta_endpoint='login')
                log_security_event('SIGNUP_EXISTING_EMAIL','BLOCKED',user_id=existing['id'],target='account')
                flash("Vérifiez votre boîte de réception : un e-mail vient de vous être envoyé pour finaliser l'accès à votre compte.")
                return redirect(url_for('login'))
            slug=re.sub(r'[^a-z0-9]+','-',norm(company)).strip('-') or 'company'; base=slug; i=1
            while c.execute('SELECT id FROM organizations WHERE slug=?',(slug,)).fetchone(): i+=1; slug=f'{base}-{i}'
            from datetime import timedelta
            trial=(datetime.now(timezone.utc)+timedelta(days=14)).isoformat()
            referral_code=secrets.token_urlsafe(6)
            c.execute("INSERT INTO organizations(name,slug,plan,status,trial_ends_at,referral_code,created_at,updated_at,terms_accepted_at,terms_version) VALUES(?,?,'TRIAL','ACTIVE',?,?,?,?,?,?)",(company,slug,trial,referral_code,now(),now(),now(),TERMS_VERSION)); oid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
            session.pop('signup_form',None)
            c.execute('INSERT INTO users(email,password_hash,full_name,is_active,created_at,updated_at) VALUES(?,?,?,1,?,?)',(email,generate_password_hash(pw),name,now(),now())); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.execute("INSERT INTO memberships(user_id,organization_id,role,created_at) VALUES(?,?,'OWNER',?)",(uid,oid,now())); c.commit()

            # Programme de parrainage : si l'inscription vient d'un lien ?ref=CODE, on lie
            # les deux organisations. La récompense (1 mois gratuit) n'est accordée que
            # lorsque l'organisation parrainée devient réellement payante (voir Stripe webhook).
            ref_code=request.args.get('ref','').strip() or session.pop('pending_ref_code','')
            if ref_code:
                referrer=c.execute('SELECT id FROM organizations WHERE referral_code=?',(ref_code,)).fetchone()
                if referrer and referrer['id']!=oid:
                    c.execute('INSERT OR IGNORE INTO referrals(referrer_org_id,referred_org_id,status,created_at) VALUES(?,?,?,?)',
                        (referrer['id'],oid,'PENDING',now())); c.commit()

            user=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close()
            acsec.start_user_session(user,oid,'OWNER'); init_tenant_db()
            tc=cx(); tc.execute("INSERT OR IGNORE INTO app_settings(id,onboarding_complete,currency,locale,notifications_enabled,created_at,updated_at) VALUES(1,0,'EUR','fr-FR',1,?,?)",(now(),now())); tc.commit(); tc.close()
            log_activity('SIGNUP',f'Création organisation {company}')
            result=send_verification_email(user)
            if result.get('sent'):
                flash('Compte créé. Un e-mail de confirmation a été envoyé.')
            elif result.get('dry_run'):
                flash("Compte créé. Vérification e-mail : service e-mail non configuré, aucun e-mail envoyé (mode simulation).")
            else:
                current_app.logger.error('Verification email failed after signup: %s', result.get('error','unknown error'))
                flash("Compte créé, mais l'email de confirmation n'a pas pu être envoyé. Utilisez « Renvoyer l'email de vérification » dans votre compte.")
            return redirect(url_for('onboarding'))
        return render_template('signup.html',form=session.pop('signup_form',{}),terms_version=TERMS_VERSION)

    @app.route('/login',methods=['GET','POST'])
    @rate_limit(8,300)
    def login():
        if session.get('user_id'): return redirect(url_for('home'))
        if request.method=='POST':
            email=request.form.get('email','').strip().lower(); pw=request.form.get('password','')
            c=auth_cx(); u=c.execute('SELECT * FROM users WHERE email=? AND is_active=1',(email,)).fetchone()
            locked_until=acsec.account_locked_until(u) if u else None
            if locked_until:
                c.close()
                acsec.dummy_password_check(pw)
                log_security_event('LOGIN_LOCKED','BLOCKED',user_id=u['id'],target='account')
                flash('Trop de tentatives infructueuses : la connexion à ce compte est temporairement bloquée. Réessayez dans quelques minutes.')
                return redirect(request.url)
            # Vérification à temps constant : une adresse inconnue calcule aussi un hachage.
            password_ok=check_password_hash(u['password_hash'],pw) if u else acsec.dummy_password_check(pw)
            if not u or not password_ok:
                failed_uid=u['id'] if u else None
                failed_org=None
                if u:
                    fm=c.execute('SELECT organization_id FROM memberships WHERE user_id=? ORDER BY id LIMIT 1',(u['id'],)).fetchone()
                    failed_org=fm['organization_id'] if fm else None
                c.close()
                log_security_event('LOGIN_FAILED','FAILURE',user_id=failed_uid,organization_id=failed_org,target='account')
                if u and acsec.register_login_failure(u):
                    log_security_event('ACCOUNT_LOCKED','BLOCKED',user_id=u['id'],organization_id=failed_org,target='account')
                flash('E-mail ou mot de passe incorrect.')
                session['login_email']=email[:254]  # l'adresse reste saisie après une erreur
                return redirect(request.url)
            m=c.execute('SELECT memberships.* FROM memberships JOIN organizations o ON o.id=memberships.organization_id '
                        'WHERE memberships.user_id=? ORDER BY (o.deleted_at IS NOT NULL), memberships.id LIMIT 1',(u['id'],)).fetchone(); c.close()
            if not m: flash('Aucune organisation associée.'); return redirect(request.url)
            acsec.reset_login_failures(u['id'])
            next_url=safe_next_url(request.args.get('next'))
            if acsec.user_has_2fa(u):
                # Second facteur exigé avant d'ouvrir la session.
                session.clear()
                session['pending_2fa']={'uid':u['id'],'org':m['organization_id'],'at':now(),'next':next_url}
                return redirect(url_for('login_two_factor'))
            session.pop('login_email',None)
            acsec.start_user_session(u,m['organization_id'],m['role']); init_tenant_db(); log_activity('LOGIN','Connexion'); log_security_event('LOGIN_SUCCESS','SUCCESS',user_id=u['id'],organization_id=m['organization_id'],target='account')
            return redirect(next_url or url_for('home'))
        return render_template('login.html',login_email=session.pop('login_email',''))

    @app.route('/logout',methods=['POST'])
    @login_required
    def logout():
        if session.get('user_id'):
            log_activity('LOGOUT','Déconnexion')
            log_security_event('LOGOUT','SUCCESS',target='account')
            # Les sessions sont des cookies signés : sans révocation côté serveur, un cookie
            # copié restait valide jusqu'à expiration après la déconnexion. Chaque session a
            # désormais un identifiant révocable : seule CETTE session (cet appareil) est
            # fermée. Une session ouverte avant cette version n'a pas d'identifiant : on
            # retombe alors sur l'incrément d'auth_version (toutes les sessions).
            if not acsec.revoke_current_session():
                c=auth_cx()
                c.execute('UPDATE users SET auth_version=COALESCE(auth_version,0)+1 WHERE id=?',(session['user_id'],))
                c.commit(); c.close()
        session.clear(); return redirect(url_for('login'))

    @app.route('/verify/<token>')
    @limiter.limit('20 per 900 seconds', override_defaults=True)
    def verify_email(token):
        u,stored_token=_token_user('verification',token)
        if not u:
            log_security_event('EMAIL_VERIFY','FAILURE',target='invalid_or_used')
            flash("Lien de vérification invalide, expiré ou déjà utilisé.")
            return redirect(url_for('login'))

        sent_at=parse_dt(u['verification_sent_at'])
        expired = (not sent_at) or (datetime.now(timezone.utc)-sent_at).total_seconds()>86400
        if expired:
            # Expiration explicite : le token inutilisable est supprimé.
            c=auth_cx()
            c.execute('UPDATE users SET verification_token=NULL WHERE id=? AND verification_token=?',(u['id'],stored_token))
            c.commit(); c.close()
            log_security_event('EMAIL_VERIFY','EXPIRED',user_id=u['id'],target='account')
            flash("Ce lien de vérification a expiré. Demandez-en un nouveau depuis votre compte.")
            return redirect(url_for('login'))

        # Consommation atomique : un même lien ne peut être validé qu'une seule fois,
        # même si deux requêtes arrivent quasiment en même temps.
        c=auth_cx()
        result=c.execute(
            'UPDATE users SET email_verified=1,verification_token=NULL,verification_sent_at=NULL,updated_at=? '
            'WHERE id=? AND verification_token=?',
            (now(),u['id'],stored_token)
        )
        c.commit(); c.close()
        if getattr(result,'rowcount',0)!=1:
            flash("Lien de vérification invalide ou déjà utilisé.")
            return redirect(url_for('login'))
        log_security_event('EMAIL_VERIFY','SUCCESS',user_id=u['id'],target='account')
        flash('Adresse e-mail vérifiée. Merci !')
        return redirect(url_for('home') if session.get('user_id') else url_for('login'))

    @app.route('/verify/resend',methods=['POST'])
    @login_required
    @rate_limit(3,900)
    def verify_resend():
        u=current_user()
        if u['email_verified']:
            flash('Votre e-mail est déjà vérifié.')
        else:
            result=send_verification_email(u)
            if result.get('sent'):
                flash("E-mail de vérification renvoyé.")
            elif result.get('dry_run'):
                flash("Service e-mail non configuré — e-mail non envoyé (mode simulation).")
            else:
                flash("L'email n'a pas pu être envoyé pour le moment. Réessayez dans quelques minutes.")
        return redirect(request.referrer or url_for('home'))

    @app.route('/forgot-password',methods=['GET','POST'])
    @rate_limit(5,900)
    def forgot_password():
        if request.method=='POST':
            email=request.form.get('email','').strip().lower()
            c=auth_cx(); u=c.execute('SELECT * FROM users WHERE email=? AND is_active=1',(email,)).fetchone(); c.close()
            if u and check_recipient_rate('password_reset',email):
                send_reset_email(u)
                log_security_event('PASSWORD_RESET_REQUEST','ACCEPTED',user_id=u['id'],target='account')
            elif u:
                # Plus de 3 demandes par heure pour cette adresse : aucun nouvel e-mail
                # (anti-harcèlement), message identique pour ne rien révéler.
                log_security_event('PASSWORD_RESET_REQUEST','THROTTLED',user_id=u['id'],target='account')
            else:
                log_security_event('PASSWORD_RESET_REQUEST','ACCEPTED',target='unknown_account')
            # Message volontairement identique que le compte existe ou non, pour ne pas révéler les emails inscrits.
            flash("Si un compte existe avec cet e-mail, un lien de réinitialisation vient d'être envoyé.")
            return redirect(url_for('login'))
        return render_template('forgot_password.html')

    @app.route('/reset-password/<token>',methods=['GET','POST'])
    @rate_limit(10,900)
    def reset_password(token):
        u,stored_token=_token_user('reset',token)
        expired=True
        if u and u['reset_token_expires']:
            exp=parse_dt(u['reset_token_expires'])
            expired = not exp or datetime.now(timezone.utc)>exp

        if not u or expired:
            if u and stored_token:
                c=auth_cx()
                c.execute('UPDATE users SET reset_token=NULL,reset_token_expires=NULL WHERE id=? AND reset_token=?',(u['id'],stored_token))
                c.commit(); c.close()
            log_security_event('PASSWORD_RESET','EXPIRED' if u else 'FAILURE',user_id=(u['id'] if u else None),target='account')
            flash("Ce lien de réinitialisation est invalide ou a expiré.")
            return redirect(url_for('forgot_password'))

        if request.method=='POST':
            pw=request.form.get('password',''); pw2=request.form.get('password_confirm','')
            err=password_error(pw,u['email'])
            if err:
                flash(err); return redirect(request.url)
            if pw!=pw2:
                flash('Les deux mots de passe ne correspondent pas.'); return redirect(request.url)

            # Consommation atomique + rotation de auth_version :
            # 1) le lien devient immédiatement inutilisable ;
            # 2) toutes les anciennes sessions de ce compte sont invalidées.
            c=auth_cx()
            result=c.execute(
                # Le lien a été reçu dans la boîte mail : l'adresse est donc prouvée.
                'UPDATE users SET password_hash=?,reset_token=NULL,reset_token_expires=NULL,email_verified=1,'
                'auth_version=COALESCE(auth_version,0)+1,password_changed_at=?,failed_login_count=0,locked_until=NULL,updated_at=? '
                'WHERE id=? AND reset_token=?',
                (generate_password_hash(pw),now(),now(),u['id'],stored_token)
            )
            c.commit(); c.close()
            if getattr(result,'rowcount',0)!=1:
                flash("Ce lien de réinitialisation a déjà été utilisé.")
                return redirect(url_for('forgot_password'))

            log_security_event('PASSWORD_RESET','SUCCESS',user_id=u['id'],target='account')
            acsec.revoke_all_sessions(u['id'])
            acsec.notify_user(u,'Mot de passe modifié',
                "Le mot de passe de votre compte ProfitOS vient d'être modifié. Si vous n'êtes pas à l'origine "
                "de ce changement, réinitialisez-le immédiatement et contactez le support.",
                cta_label='Réinitialiser mon mot de passe',cta_endpoint='forgot_password')
            # La session courante, si elle existe, ne doit pas survivre au changement.
            session.clear()
            flash('Mot de passe mis à jour. Toutes les anciennes sessions ont été invalidées. Vous pouvez vous reconnecter.')
            return redirect(url_for('login'))

        return render_template('reset_password.html',token=token)

    @app.route('/onboarding',methods=['GET','POST'])
    @login_required
    def onboarding():
        from profitos.company_profile import parse_company_form, save_company_profile, LEGAL_FORMS, TEXT_FIELDS
        c=cx(); p=c.execute('SELECT * FROM company WHERE id=1').fetchone(); c.close(); org=current_org()
        errors=[]
        if request.method=='POST':
            # Étape « Identité de l'entreprise » (SIRET, adresse, TVA, forme juridique) + profil
            # marchés publics facultatif. Mêmes contrôles que le profil entreprise.
            merged={k:((p[k] if p and k in p.keys() else '') or '') for k in TEXT_FIELDS}
            form=dict(request.form)
            form['name']=request.form.get('company_name','').strip() or request.form.get('name','').strip()
            for k in TEXT_FIELDS:
                if k in form: merged[k]=form[k]
            values,errors=parse_company_form(merged)
            if not errors:
                c=cx()
                save_company_profile(c,values)
                c.execute("INSERT INTO app_settings(id,onboarding_complete,currency,locale,notifications_enabled,created_at,updated_at) VALUES(1,1,'EUR','fr-FR',1,?,?) ON CONFLICT(id) DO UPDATE SET onboarding_complete=1,updated_at=excluded.updated_at",(now(),now()))
                c.commit(); c.close()
                # Le nom saisi devient aussi le nom de l'organisation (barre latérale, e-mails).
                if values['name'] and org:
                    ac=auth_cx(); ac.execute('UPDATE organizations SET name=?,updated_at=? WHERE id=?',(values['name'][:200],now(),org['id'])); ac.commit(); ac.close()
                log_activity('ONBOARDING_COMPLETE','Profil entreprise complété')
                flash('Configuration enregistrée. Vous pouvez importer vos premières factures ou en créer une.')
                return redirect(url_for('home'))
            p=values
        return render_template('onboarding.html',profile=p,org=org,errors=errors,legal_forms=LEGAL_FORMS),(400 if errors else 200)

    @app.route('/integrations',methods=['GET','POST'])
    @login_required
    @require_area('settings')
    def integrations():
        """Aucune intégration comptable réelle n'est encore branchée (nécessite un vrai
        compte partenaire/OAuth chez Pennylane, QuickBooks ou Sage). Cette page capture
        l'intérêt pour prioriser le développement, plutôt que de simuler une fausse connexion."""
        org=current_org()
        if request.method=='POST':
            provider=request.form.get('provider','').strip()
            allowed_providers={'Pennylane','QuickBooks','Sage'}
            if provider not in allowed_providers:
                abort(400)
            email=current_user()['email']
            c=auth_cx()
            existing=c.execute(
                'SELECT 1 FROM integration_interest WHERE organization_id=? AND provider=?',
                (org['id'],provider),
            ).fetchone()
            if not existing:
                c.execute('INSERT INTO integration_interest(organization_id,provider,email,created_at) VALUES(?,?,?,?)',
                    (org['id'],provider,email,now()))
                c.commit()
                log_activity('INTEGRATION_INTEREST',f'Intérêt exprimé pour {provider}')
            c.close()
            flash(f"Merci ! Nous vous préviendrons dès que la connexion {provider} sera disponible.")
            return redirect(url_for('integrations'))
        ac=auth_cx(); requested=ac.execute('SELECT provider FROM integration_interest WHERE organization_id=?',(org['id'],)).fetchall(); ac.close()
        requested_providers={r['provider'] for r in requested}
        return render_template('integrations.html',requested_providers=requested_providers)

    @app.route('/referral')
    @login_required
    def referral():
        org=current_org()
        if not org['referral_code']:
            code=secrets.token_urlsafe(6)
            c=auth_cx(); c.execute('UPDATE organizations SET referral_code=? WHERE id=?',(code,org['id'])); c.commit(); c.close()
            org=current_org()
        ac=auth_cx()
        referred=ac.execute('SELECT r.*,o.name FROM referrals r JOIN organizations o ON o.id=r.referred_org_id WHERE r.referrer_org_id=? ORDER BY r.id DESC',(org['id'],)).fetchall()
        ac.close()
        base_url=app_base_url()
        referral_link=f"{base_url}{url_for('signup')}?ref={org['referral_code']}"
        return render_template('referral.html',org=org,referred=referred,referral_link=referral_link)

    @app.route('/settings',methods=['GET','POST'])
    @login_required
    @require_area('settings')
    def settings():
        if request.method=='POST':
            currency=request.form.get('currency','EUR'); locale=request.form.get('locale','fr-FR'); notif=1 if request.form.get('notifications_enabled')=='on' else 0
            slack_url=request.form.get('slack_webhook_url','').strip() or None; teams_url=request.form.get('teams_webhook_url','').strip() or None
            if slack_url and not validate_webhook_url(slack_url): flash('Webhook Slack invalide ou non autorisé.'); return redirect(request.url)
            if teams_url and not validate_webhook_url(teams_url): flash('Webhook Teams invalide ou non autorisé.'); return redirect(request.url)
            accountant_email=request.form.get('accountant_email','').strip() or None
            weekly_export=1 if request.form.get('weekly_export_enabled')=='on' else 0
            logo_url=request.form.get('logo_url','').strip() or None
            accent_color=request.form.get('accent_color','').strip() or None
            if logo_url and not validate_logo_url(logo_url): flash('URL de logo invalide (https requis).'); return redirect(request.url)
            if accent_color and not validate_hex_color(accent_color): flash('Couleur invalide : utilisez un format hexadécimal (ex. : #111827).'); return redirect(request.url)
            require_validation=1 if request.form.get('require_purchase_validation')=='on' else 0
            c=cx(); c.execute("INSERT INTO app_settings(id,onboarding_complete,currency,locale,notifications_enabled,slack_webhook_url,teams_webhook_url,accountant_email,weekly_export_enabled,logo_url,accent_color,require_purchase_validation,created_at,updated_at) VALUES(1,1,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET currency=excluded.currency,locale=excluded.locale,notifications_enabled=excluded.notifications_enabled,slack_webhook_url=excluded.slack_webhook_url,teams_webhook_url=excluded.teams_webhook_url,accountant_email=excluded.accountant_email,weekly_export_enabled=excluded.weekly_export_enabled,logo_url=excluded.logo_url,accent_color=excluded.accent_color,require_purchase_validation=excluded.require_purchase_validation,updated_at=excluded.updated_at",(currency,locale,notif,slack_url,teams_url,accountant_email,weekly_export,logo_url,accent_color,require_validation,now(),now())); c.commit(); c.close()
            # Benchmark anonymisé des délais de paiement : participation volontaire (désactivée par défaut).
            opt_in=1 if request.form.get('benchmark_opt_in')=='on' else 0
            ac=auth_cx(); ac.execute('UPDATE organizations SET benchmark_opt_in=? WHERE id=?',(opt_in,session['org_id']))
            if not opt_in: ac.execute('DELETE FROM sector_dso_signals WHERE organization_id=?',(session['org_id'],))
            ac.commit(); ac.close()
            log_activity('SETTINGS_UPDATE','Paramètres mis à jour'); flash('Paramètres enregistrés.'); return redirect(url_for('settings'))
        c=cx(); s=c.execute('SELECT * FROM app_settings WHERE id=1').fetchone(); c.close(); ac=auth_cx(); activity=ac.execute('SELECT * FROM activity_log WHERE organization_id=? ORDER BY id DESC LIMIT 30',(session['org_id'],)).fetchall(); ac.close(); return render_template('settings.html',app_settings=s,org=current_org(),user=current_user(),activity=activity)

    @app.route('/settings/send-accountant-export',methods=['POST'])
    @login_required
    @require_area('settings')
    def send_accountant_export_now():
        """Envoie immédiatement (test manuel) l'export RECOVER au comptable configuré.
        N'envoie qu'un LIEN de téléchargement sécurisé par token, jamais de pièce jointe
        brute — le lien régénère l'export à la demande, toujours à jour."""
        org=current_org()
        c=cx(); s=c.execute('SELECT * FROM app_settings WHERE id=1').fetchone(); c.close()
        if not s or not s['accountant_email']:
            flash("Renseignez d'abord l'e-mail de votre comptable dans Paramètres."); return redirect(url_for('settings'))
        result=send_accountant_export(org,s['accountant_email'])
        if result.get('dry_run'):
            flash(f"SMTP/Resend non configuré — e-mail non envoyé (mode simulation) à {s['accountant_email']}.")
        elif result.get('sent'):
            flash(f"Export envoyé à {s['accountant_email']}.")
        else:
            flash(f"Échec de l'envoi : {result.get('error') if result.get('blocked') else 'le service e-mail est indisponible, réessayez plus tard.'}")
        return redirect(url_for('settings'))

    @app.route('/settings/security-events')
    @login_required
    def security_events():
        if current_role()!='OWNER':
            abort(403)
        ensure_security_events_table()
        c=auth_cx()
        try:
            rows=c.execute(
                'SELECT id,event_type,outcome,user_id,organization_id,target,created_at '
                'FROM security_events WHERE organization_id=? ORDER BY id DESC LIMIT 200',
                (session['org_id'],)
            ).fetchall()
            ids=sorted({r['user_id'] for r in rows if r['user_id']})
            user_emails={}
            if ids:
                q='SELECT id,email FROM users WHERE id IN (%s)'%','.join('?'*len(ids))
                user_emails={r['id']:r['email'] for r in c.execute(q,ids).fetchall()}
        finally:
            c.close()
        return render_template('security_events.html',events=rows,user_emails=user_emails)

    @app.route('/settings/audit-export')
    @login_required
    def audit_export():
        """Export d'un journal d'audit combiné (activité + historique de statuts) —
        utile pour un cabinet comptable qui a besoin d'une trace conforme des actions."""
        if current_role() not in ('OWNER','ADMIN'):
            flash("Seuls le propriétaire ou un administrateur peuvent exporter le journal d'audit.")
            return redirect(url_for('settings'))
        ac=auth_cx()
        activity=ac.execute('SELECT event_type,description,created_at FROM activity_log WHERE organization_id=? ORDER BY id DESC LIMIT 1000',(session['org_id'],)).fetchall()
        ac.close()
        c=cx()
        status=c.execute('SELECT entity_type,entity_id,kind,old_status,new_status,changed_by,note,created_at FROM status_history ORDER BY id DESC LIMIT 1000').fetchall()
        c.close()

        rows=[]
        for a in activity:
            rows.append({'Date':a['created_at'],'Catégorie':'ACTIVITY','Type':a['event_type'],
                         'Détail':a['description'],'Ancien statut':'','Nouveau statut':'','Auteur':''})
        for s in status:
            rows.append({'Date':s['created_at'],'Catégorie':'STATUS_CHANGE','Type':f"{s['entity_type']}/{s['kind']}",
                         'Détail':s['note'] or f"#{s['entity_id']}",'Ancien statut':s['old_status'] or '',
                         'Nouveau statut':s['new_status'],'Auteur':s['changed_by']})
        rows.sort(key=lambda r:r['Date'] or '',reverse=True)
        log_activity('AUDIT_EXPORT','Export du journal d\'audit')
        return export_response(rows,'profitos-audit-log')

    @app.route('/settings/test-notification',methods=['POST'])
    @login_required
    @require_area('settings')
    def test_notification():
        notify_org(f"🔔 Test ProfitOS depuis {current_org()['name']} — les notifications fonctionnent.")
        flash("Notification de test envoyée (si un webhook est configuré et joignable).")
        return redirect(url_for('settings'))

    @app.route('/team')
    @login_required
    @require_area('team')
    def team():
        c=auth_cx(); rows=c.execute('SELECT memberships.*,users.email,users.full_name FROM memberships JOIN users ON users.id=memberships.user_id WHERE memberships.organization_id=? ORDER BY memberships.id',(session['org_id'],)).fetchall()
        pending=c.execute('SELECT id,email,role,created_at,expires_at FROM team_invitations WHERE organization_id=? AND accepted_at IS NULL '
                          'AND revoked_at IS NULL AND expires_at>? ORDER BY id DESC',(session['org_id'],now())).fetchall()
        org=c.execute('SELECT require_2fa FROM organizations WHERE id=?',(session['org_id'],)).fetchone()
        c.close()
        return render_template('team.html',members=rows,pending_invites=pending,require_2fa=acsec.org_requires_2fa(org))

    @app.route('/team/<int:uid>/entites', methods=['GET', 'POST'])
    @login_required
    @require_area('team')
    def team_entity_access(uid):
        if current_role() != 'OWNER':
            flash("Seul le propriétaire peut gérer les accès par entité.")
            return redirect(url_for('team'))
        ac = auth_cx()
        member = ac.execute(
            'SELECT memberships.*,users.email,users.full_name FROM memberships JOIN users ON users.id=memberships.user_id '
            'WHERE memberships.organization_id=? AND memberships.user_id=?', (session['org_id'], uid),
        ).fetchone()
        ac.close()
        if not member:
            flash("Membre introuvable.")
            return redirect(url_for('team'))

        from profitos.entities import list_all_entities, user_has_entity_restrictions
        c = cx()
        if request.method == 'POST':
            if request.form.get('mode') == 'unrestricted':
                c.execute('DELETE FROM user_entity_access WHERE user_id=?', (uid,))
            else:
                checked_raw = request.form.getlist('entity_id')
                # Seules les entités existantes sont acceptées : une valeur altérée ou inconnue
                # provoquait une erreur 500 (int('abc')) ou un accès à une entité inexistante.
                known = {str(e['id']) for e in list_all_entities(c) if e['id'] is not None}
                checked = {(int(v) if v else None) for v in checked_raw if not v or v in known}
                if not checked:
                    # Aucune ligne = accès à TOUTES les entités : enregistrer une restriction
                    # vide ouvrirait tout le dossier au lieu de le fermer.
                    c.close()
                    flash("Cochez au moins une entité, ou choisissez « Accès à toutes les entités ». "
                          "Pour retirer tout accès, retirez le membre de l'équipe.")
                    return redirect(url_for('team_entity_access', uid=uid))
                c.execute('DELETE FROM user_entity_access WHERE user_id=?', (uid,))
                for eid in checked:
                    c.execute(
                        'INSERT INTO user_entity_access(user_id,entity_id,created_at) VALUES(?,?,?)',
                        (uid, eid, now()),
                    )
            c.commit(); c.close()
            log_activity('TEAM_ENTITY_ACCESS_UPDATED', f"Accès par entité modifié pour {member['email']}")
            flash("Accès par entité mis à jour.")
            return redirect(url_for('team'))

        entities = list_all_entities(c)
        restricted = user_has_entity_restrictions(c, uid)
        allowed_ids = set()
        if restricted:
            allowed_ids = {r['entity_id'] for r in c.execute('SELECT entity_id FROM user_entity_access WHERE user_id=?', (uid,)).fetchall()}
        c.close()
        return render_template('team_entity_access.html', member=member, entities=entities,
                                restricted=restricted, allowed_ids=allowed_ids)

    @app.route('/settings/theme',methods=['POST'])
    @login_required
    def toggle_theme():
        current=current_user()['theme_preference'] or 'dark'
        new_theme='light' if current=='dark' else 'dark'
        c=auth_cx(); c.execute('UPDATE users SET theme_preference=? WHERE id=?',(new_theme,session['user_id'])); c.commit(); c.close()
        return redirect(request.referrer or url_for('home'))

    @app.route('/settings/gdpr-export')
    @login_required
    @require_area('settings')
    def gdpr_export():
        """Export RGPD de l'organisation (propriétaire / administrateur) : archive ZIP
        contenant TOUTES les tables de l'organisation en CSV (séparateur « ; »), plus
        ses métadonnées de compte. Lecture seule. Les secrets (jetons, mots de passe,
        identifiants d'intégration) sont masqués."""
        from profitos.gdpr_export import build_organization_export
        org=current_org()
        data=build_organization_export(org['id'])
        log_activity('GDPR_EXPORT','Export RGPD de l\'organisation téléchargé')
        log_security_event('GDPR_EXPORT','SUCCESS',target='organization')
        return send_file(io.BytesIO(data),mimetype='application/zip',as_attachment=True,
                         download_name=f"profitos-export-organisation-{org['id']}-{date.today().isoformat()}.zip",
                         max_age=0)

    @app.route('/settings/delete-account',methods=['GET','POST'])
    @login_required
    def delete_account():
        """Suppression de compte / départ d'organisation.
        - Si l'utilisateur est le DERNIER membre de l'organisation active : la suppression
          de l'organisation est PROGRAMMÉE (délai de grâce de 14 jours pendant lequel elle
          est inaccessible mais restaurable), puis exécutée par
          profitos.retention.purge_deleted_organizations() (tâche planifiée).
        - Sinon : seule l'adhésion de l'utilisateur à cette organisation est retirée
          (il quitte l'organisation, les données des autres membres restent intactes).
        - Un propriétaire unique ne peut pas partir sans avoir d'abord promu quelqu'un
          d'autre au rôle Propriétaire (évite une organisation orpheline).
        - Le mot de passe actuel est exigé (ré-authentification)."""
        from profitos.retention import schedule_organization_deletion, GRACE_PERIOD_DAYS
        org=current_org(); user=current_user()
        ac=auth_cx()
        member_count=ac.execute('SELECT COUNT(*) c FROM memberships WHERE organization_id=?',(org['id'],)).fetchone()['c']

        if request.method=='POST':
            confirm_text=request.form.get('confirm_text','').strip()
            if confirm_text!=org['name']:
                ac.close()
                flash("Le nom saisi ne correspond pas exactement au nom de l'organisation.")
                return redirect(request.url)
            if not acsec.verify_reauth(user):
                ac.close()
                flash("Mot de passe incorrect : la suppression n'a pas été effectuée.")
                return redirect(request.url)

            if current_role()=='OWNER' and member_count>1:
                other_owners=ac.execute("SELECT COUNT(*) c FROM memberships WHERE organization_id=? AND role='OWNER' AND user_id!=?",(org['id'],user['id'])).fetchone()['c']
                if other_owners==0:
                    ac.close()
                    flash("Vous êtes le seul propriétaire de cette organisation. Attribuez d'abord le rôle Propriétaire à un autre membre (page Équipe) avant de partir.")
                    return redirect(url_for('team'))

            if member_count==1:
                # Abonnement Stripe : l'annuler AVANT de programmer la suppression, sinon le client
                # continue d'être prélevé sans plus avoir de compte (et les webhooks ne retrouvent
                # plus l'organisation). En cas d'échec, rien n'est programmé.
                sub_id=(org['stripe_subscription_id'] or '').strip()
                if sub_id and org['status'] in ('ACTIVE_PAID','PAST_DUE'):
                    if not BILLING_ENABLED:
                        ac.close()
                        flash("Votre abonnement est encore actif et ne peut pas être résilié automatiquement pour le moment. Contactez le support avant de supprimer le compte.")
                        return redirect(url_for('settings'))
                    try:
                        get_stripe().Subscription.cancel(sub_id)
                    except Exception as exc:
                        if 'No such subscription' not in str(exc) and 'canceled' not in str(exc).lower():
                            ac.close()
                            current_app.logger.exception('Stripe cancel failed for org %s',org['id'])
                            flash("La résiliation de votre abonnement a échoué : rien n'a été supprimé. Réessayez dans quelques minutes ou contactez le support.")
                            return redirect(url_for('settings'))
                ac.close()
                purge_at=schedule_organization_deletion(org['id'],user['id'])
                log_activity('ORG_DELETION_SCHEDULED',f"Suppression de l'organisation programmée (purge le {purge_at.date().isoformat()})")
                log_security_event('ORG_DELETION_SCHEDULED','SUCCESS',target='organization')
                acsec.revoke_current_session()
                session.clear()
                flash(f"La suppression de l'organisation est programmée : elle est désormais inaccessible et ses données "
                      f"seront définitivement effacées le {fr_date(purge_at.date().isoformat())} ({GRACE_PERIOD_DAYS} jours). "
                      "Pour l'annuler d'ici là, reconnectez-vous.")
                return redirect(url_for('login'))

            # L'organisation continue d'exister : seul l'utilisateur la quitte.
            ac.execute('DELETE FROM memberships WHERE user_id=? AND organization_id=?',(user['id'],org['id']))
            ac.commit()
            remaining_orgs=ac.execute('SELECT COUNT(*) c FROM memberships WHERE user_id=?',(user['id'],)).fetchone()['c']
            if remaining_orgs==0:
                for table in ('push_device_tokens','user_sessions','user_devices'):
                    ac.execute(f'DELETE FROM {table} WHERE user_id=?',(user['id'],))
                ac.execute('DELETE FROM users WHERE id=?',(user['id'],)); ac.commit()
            ac.close()
            log_activity('ACCOUNT_LEFT_ORG',f"Utilisateur retiré de l'organisation : {user['email']}")
            session.clear()
            flash('Vous avez quitté cette organisation.' + (' Votre compte utilisateur a été supprimé.' if remaining_orgs==0 else ''))
            return redirect(url_for('login'))

        ac.close()
        return render_template('delete_account.html',org=org,is_last_member=(member_count==1),
                               grace_days=GRACE_PERIOD_DAYS,can_export_accounting=can_access('accounting'),
                               can_export_org=can_access('settings'))

    @app.route('/changelog/seen',methods=['POST'])
    @login_required
    def changelog_seen():
        if CHANGELOG_ENTRIES:
            c=auth_cx(); c.execute('UPDATE users SET last_seen_changelog=? WHERE id=?',(CHANGELOG_ENTRIES[0]['date'],session['user_id'])); c.commit(); c.close()
        return redirect(request.referrer or url_for('home'))

    @app.route('/team/performance')
    @login_required
    @require_area('team')
    def team_performance():
        """Compte, par personne (via changed_by de status_history), le nombre de
        changements de statut effectués — approbations, envois, résolutions..."""
        c=cx()
        rows=c.execute("SELECT changed_by,COUNT(*) n FROM status_history WHERE changed_by IS NOT NULL AND changed_by!='system' GROUP BY changed_by ORDER BY n DESC").fetchall()
        recovered=c.execute("SELECT changed_by,COUNT(*) n FROM status_history WHERE new_status='paid' AND changed_by IS NOT NULL GROUP BY changed_by").fetchall()
        c.close()
        recovered_map={r['changed_by']:r['n'] for r in recovered}
        stats=[{'name':r['changed_by'],'total_changes':r['n'],'invoices_recovered':recovered_map.get(r['changed_by'],0)} for r in rows]
        return render_template('team_performance.html',stats=stats)

    def send_invite_email(email, org, role, raw_token, existing_user, dry_run=None):
        """Invitation : lien d'acceptation à usage unique, valable 7 jours. Le compte
        existant n'est JAMAIS modifié (ni mot de passe, ni sessions) : la personne
        accepte en se connectant avec cette adresse, ou crée son compte via le lien."""
        inviter=current_user()
        link=f"{app_base_url()}{url_for('team_invitation',token=raw_token)}"
        intro=(f"{inviter['full_name'] or inviter['email']} ({inviter['email']}) vous invite à rejoindre l'organisation "
               f"« {org['name']} » sur ProfitOS avec le rôle « {ROLE_LABELS.get(role,role)} ». ")
        intro+=("Connectez-vous avec cette adresse e-mail pour accepter l'invitation." if existing_user
                else "Cliquez ci-dessous pour créer votre compte et rejoindre l'organisation.")
        html=render_template('email_transactional.html',title='Invitation à rejoindre une organisation',
            intro=intro,cta_label="Voir l'invitation",cta_url=link,
            footer="Ce lien est valable 7 jours. Si vous ne connaissez pas l'expéditeur, ignorez ce message : rien ne sera fait sans votre accord.")
        # Objet fixe : jamais le nom d'organisation choisi par l'expéditeur (anti-hameçonnage).
        return send_email(email,"Invitation à rejoindre une organisation sur ProfitOS",html,dry_run=dry_run,kind='invite')

    @app.route('/team/invite',methods=['POST'])
    @login_required
    @require_area('team')
    @rate_limit(10,3600)
    def team_invite():
        if current_role()!='OWNER':
            flash("Seul le propriétaire peut inviter des membres."); return redirect(url_for('team'))
        email=request.form.get('email','').strip().lower(); role=request.form.get('role','MEMBER').upper()
        if role not in ROLES or role=='OWNER': role='MEMBER'
        if not email or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email) or len(email)>254:
            flash('Adresse e-mail invalide.'); return redirect(url_for('team'))
        quota_error=check_outbound_email_quota('invite')
        if quota_error:
            flash(quota_error); return redirect(url_for('team'))
        c=auth_cx()

        org=current_org()
        plan=effective_plan(org) if org else 'TRIAL'
        member_count=c.execute(
            'SELECT COUNT(*) AS n FROM memberships WHERE organization_id=?',
            (session['org_id'],)
        ).fetchone()['n']
        pending_count=c.execute(
            'SELECT COUNT(*) AS n FROM team_invitations WHERE organization_id=? AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at>?',
            (session['org_id'],now())
        ).fetchone()['n']

        if not within_limit(plan,'team_members',member_count+pending_count,1):
            c.close()
            limit=plan_limit(plan,'team_members')
            log_security_event('PLAN_LIMIT','BLOCKED',organization_id=session['org_id'],target='team_members')
            flash(
                f"Votre formule {plan} autorise au maximum {pluralize(limit, 'utilisateur')} dans l'équipe. "
                "Passez à une formule supérieure pour inviter davantage de membres."
            )
            return redirect(url_for('team'))

        already=c.execute('SELECT 1 FROM memberships JOIN users ON users.id=memberships.user_id WHERE users.email=? AND memberships.organization_id=?',(email,session['org_id'])).fetchone()
        if already: c.close(); flash('Cette personne est déjà membre de cette organisation.'); return redirect(url_for('team'))
        existing_user=c.execute('SELECT id FROM users WHERE email=? AND is_active=1',(email,)).fetchone()
        raw_token=gen_token()
        expires=(datetime.now(timezone.utc)+timedelta(days=7)).isoformat()
        # Une seule invitation active par adresse : la précédente est révoquée.
        c.execute('UPDATE team_invitations SET revoked_at=? WHERE organization_id=? AND email=? AND accepted_at IS NULL AND revoked_at IS NULL',
                  (now(),session['org_id'],email))
        c.execute('INSERT INTO team_invitations(organization_id,email,role,token_hash,invited_by,created_at,expires_at) VALUES(?,?,?,?,?,?,?)',
                  (session['org_id'],email,role,token_digest(raw_token),session['user_id'],now(),expires))
        c.commit(); c.close()
        result=send_invite_email(email,org,role,raw_token,bool(existing_user))
        log_activity('TEAM_INVITE',f'Invitation envoyée à {email} ({ROLE_LABELS.get(role,role)})')
        log_security_event('TEAM_INVITE','SUCCESS',user_id=session.get('user_id'),target=f'invite:{email[:3]}***')
        if result.get('sent'):
            flash(f"Invitation envoyée à {email}. La personne rejoindra l'équipe après avoir accepté.")
        elif result.get('dry_run'):
            flash(f"Invitation créée. Service e-mail non configuré — e-mail non envoyé (mode simulation) à {email}.")
        elif result.get('blocked'):
            flash(result.get('error'))
        else:
            flash(f"Invitation créée, mais l'e-mail n'a pas pu être envoyé à {email}. Vous pouvez la révoquer et réessayer.")
        return redirect(url_for('team'))

    @app.route('/team/invitations/<int:invite_id>/revoquer',methods=['POST'])
    @login_required
    @require_area('team')
    def team_invite_revoke(invite_id):
        if current_role()!='OWNER':
            flash("Seul le propriétaire peut révoquer une invitation."); return redirect(url_for('team'))
        c=auth_cx()
        r=c.execute('UPDATE team_invitations SET revoked_at=? WHERE id=? AND organization_id=? AND accepted_at IS NULL AND revoked_at IS NULL',
                    (now(),invite_id,session['org_id']))
        c.commit(); c.close()
        if getattr(r,'rowcount',0)!=1:
            flash("Invitation introuvable ou déjà utilisée."); return redirect(url_for('team'))
        log_security_event('TEAM_INVITE_REVOKED','SUCCESS',target=f'invite:{invite_id}')
        flash("Invitation révoquée.")
        return redirect(url_for('team'))

    @app.route('/team/<int:uid>/role',methods=['POST'])
    @login_required
    @require_area('team')
    def team_set_role(uid):
        if current_role()!='OWNER':
            flash("Seul le propriétaire peut modifier les rôles."); return redirect(url_for('team'))
        role=request.form.get('role','MEMBER').upper()
        if role not in ROLES: role='MEMBER'
        if uid==session['user_id'] and role!='OWNER':
            flash("Vous ne pouvez pas retirer votre propre rôle de propriétaire."); return redirect(url_for('team'))
        c=auth_cx(); c.execute('UPDATE memberships SET role=? WHERE user_id=? AND organization_id=?',(role,uid,session['org_id'])); c.commit(); c.close()
        log_activity('TEAM_ROLE_UPDATE',f'Rôle mis à jour pour user #{uid} : {ROLE_LABELS.get(role,role)}')
        log_security_event('TEAM_ROLE_UPDATE','SUCCESS',target=f'user:{uid}')
        flash('Rôle mis à jour.'); return redirect(url_for('team'))

    @app.route('/team/<int:uid>/remove',methods=['POST'])
    @login_required
    @require_area('team')
    def team_remove(uid):
        if current_role()!='OWNER':
            flash("Seul le propriétaire peut retirer des membres."); return redirect(url_for('team'))
        if uid==session['user_id']:
            flash("Vous ne pouvez pas vous retirer vous-même."); return redirect(url_for('team'))
        if not acsec.verify_reauth(current_user()):
            flash("Mot de passe incorrect : confirmez votre mot de passe pour retirer un membre."); return redirect(url_for('team'))
        c=auth_cx(); c.execute('DELETE FROM memberships WHERE user_id=? AND organization_id=?',(uid,session['org_id']))
        if not c.execute('SELECT 1 FROM memberships WHERE user_id=? LIMIT 1',(uid,)).fetchone():
            # Plus aucune organisation : appareils de notification supprimés, compte
            # anonymisé après 30 jours (retention.anonymize_orphan_users).
            c.execute('DELETE FROM push_device_tokens WHERE user_id=?',(uid,))
            c.execute('UPDATE users SET membership_lost_at=? WHERE id=?',(now(),uid))
        c.commit(); c.close()
        log_activity('TEAM_REMOVE',f'Membre #{uid} retiré')
        log_security_event('TEAM_REMOVE','SUCCESS',target=f'user:{uid}')
        flash('Membre retiré de l\'organisation.'); return redirect(url_for('team'))

    # Garde global : abonnement actif/essai valide + formule, pour toutes les
    # routes connectées (liste d'exceptions dans profitos/feature_access.py).
    from profitos.feature_access import enforce_subscription_access
    app.before_request(enforce_subscription_access)

    def _billing_owner_contact(payer):
        if not payer: return None
        acx=auth_cx()
        try:
            return acx.execute(
                "SELECT u.full_name,u.email FROM memberships m JOIN users u ON u.id=m.user_id "
                "WHERE m.organization_id=? AND m.role='OWNER' ORDER BY m.id LIMIT 1",
                (payer['id'],)
            ).fetchone()
        finally:
            acx.close()

    def _open_billing_portal(stripe, org, customer_id=None):
        customer_id=customer_id or org['stripe_customer_id']
        if not stripe or not customer_id:
            flash("Aucun abonnement Stripe associé à cette organisation.")
            return redirect(url_for('billing'))
        base=app_base_url().rstrip('/')
        try:
            portal=stripe.billing_portal.Session.create(customer=customer_id,return_url=f'{base}{url_for("billing")}')
        except Exception:
            current_app.logger.exception('Stripe portal failure request_id=%s org_id=%s',getattr(g,'request_id','-'),org['id'])
            log_security_event('BILLING_PORTAL','FAILURE',organization_id=org['id'],target='billing')
            flash("Impossible d'ouvrir le portail de facturation pour le moment.")
            return redirect(url_for('billing'))
        log_security_event('BILLING_PORTAL','SUCCESS',organization_id=org['id'],target='billing')
        return redirect(portal.url,code=303)

    def _stripe_has_live_subscription(stripe, customer_id):
        """True si Stripe connaît déjà un abonnement actif, en essai ou impayé pour
        ce client. Une erreur d'API est propagée (on ne crée jamais de paiement
        à l'aveugle)."""
        subs=stripe.Subscription.list(customer=customer_id,status='all',limit=20)
        for sub in (subs.get('data') if hasattr(subs,'get') else subs) or []:
            if (sub.get('status') or '') in ('active','trialing','past_due'):
                return True
        return False

    def _expire_open_checkout_sessions(stripe, customer_id):
        try:
            sessions=stripe.checkout.Session.list(customer=customer_id,status='open',limit=20)
            for cs in (sessions.get('data') if hasattr(sessions,'get') else sessions) or []:
                try:
                    stripe.checkout.Session.expire(cs['id'])
                except Exception:
                    current_app.logger.warning('Stripe checkout session expire failed id=%s',cs.get('id'))
        except Exception:
            current_app.logger.warning('Stripe open checkout sessions lookup failed customer=%s',customer_id)

    @app.route('/abonnement-inactif')
    @login_required
    def subscription_inactive():
        """Page accessible à tous les rôles (pas de require_area) : évite la boucle
        /billing (réservé au propriétaire) -> / -> /billing."""
        org=current_org()
        has_access=org_has_access(org)
        reason='formule' if request.args.get('raison')=='formule' else 'inactif'
        if reason=='inactif' and has_access:
            return redirect(url_for('home'))
        payer=billing_org(org) if org else None
        is_child=bool(payer and org and payer['id']!=org['id'])
        is_owner=current_role()=='OWNER' and not is_child
        grace_end=past_due_grace_end(payer)
        return render_template(
            'subscription_inactive.html',
            org=org,
            payer=payer,
            is_child=is_child,
            is_owner=is_owner,
            owner_contact=_billing_owner_contact(payer),
            state=subscription_state(org),
            reason=reason,
            grace_end=grace_end.strftime('%d/%m/%Y') if grace_end else None,
        )

    @app.route('/billing')
    @login_required
    @require_area('billing')
    def billing():
        org=current_org()
        plans={k:v for k,v in STRIPE_PLANS.items() if v.get('price_id')}
        payer=billing_org(org)
        is_child=bool(payer and payer['id']!=org['id'])

        cancellation_scheduled=False
        cancellation_date=None

        renewal_date=None
        stripe=get_stripe()
        sub_id=payer['stripe_subscription_id'] if payer and not is_child else None

        if stripe and sub_id and payer['status'] in ('ACTIVE_PAID','PAST_DUE'):
            try:
                subscription=stripe.Subscription.retrieve(sub_id)
                cancellation_scheduled=bool(subscription.get('cancel_at_period_end'))

                period_end=subscription.get('current_period_end')
                if period_end:
                    try:
                        cancellation_date=datetime.fromtimestamp(
                            int(period_end),
                            tz=timezone.utc
                        ).strftime('%d/%m/%Y')
                    except (TypeError,ValueError,OverflowError):
                        cancellation_date=None
                renewal_date=cancellation_date

                if cancellation_scheduled:
                    if cancellation_date:
                        flash(
                            f"Annulation programmée le {fr_date(cancellation_date)}. "
                            "Vous conservez l'accès à votre abonnement jusqu'à cette date."
                        )
                    else:
                        flash(
                            "L'annulation de votre abonnement est programmée à la fin "
                            "de la période de facturation en cours."
                        )

            except Exception:
                current_app.logger.exception(
                    'Stripe subscription status lookup failure request_id=%s org_id=%s',
                    getattr(g,'request_id','-'),
                    org['id'] if org else None
                )

        # Utilisation et limites du plan courant (plan du parent Multi-sociétés).
        current_plan=effective_plan(org) if org else 'TRIAL'
        import_usage=quota_state(
            'imports_per_month',
            organization_id=org['id'] if org else None,
            plan=current_plan,
        )
        report_usage=quota_state(
            'reports_per_month',
            organization_id=org['id'] if org else None,
            plan=current_plan,
        )

        acx=auth_cx()
        try:
            team_members=acx.execute(
                'SELECT COUNT(*) AS n FROM memberships WHERE organization_id=?',
                (org['id'],)
            ).fetchone()['n'] if org else 0

            if (plan_limit(current_plan,'organizations') or 0)>1:
                organizations_owned=acx.execute(
                    'SELECT COUNT(*) AS n FROM organizations WHERE id=? OR parent_org_id=?',
                    (payer['id'],payer['id'])
                ).fetchone()['n']
            else:
                organizations_owned=acx.execute(
                    "SELECT COUNT(*) AS n FROM memberships WHERE user_id=? AND role='OWNER'",
                    (session['user_id'],)
                ).fetchone()['n']
        finally:
            acx.close()

        quota_usage={
            'imports': import_usage,
            'reports': report_usage,
            'ai': quota_state(
                'ai_extractions_per_month',
                organization_id=payer['id'] if payer else None,
                plan=current_plan,
            ),
            'team': {
                'used': int(team_members),
                'limit': plan_limit(current_plan,'team_members'),
            },
            'organizations': {
                'used': int(organizations_owned),
                'limit': plan_limit(current_plan,'organizations'),
            },
        }

        return render_template(
            'billing.html',
            org=org,
            billing_enabled=BILLING_ENABLED,
            trial_days=trial_days_left(org),
            has_access=org_has_access(org),
            plans=plans,
            stripe_publishable_key=STRIPE_PUBLISHABLE_KEY,
            cancellation_scheduled=cancellation_scheduled,
            cancellation_date=cancellation_date,
            plan_limits=PLAN_LIMITS,
            quota_usage=quota_usage,
            catalog=STRIPE_PLANS,
            feature_labels=FEATURE_LABELS,
            payer=payer,
            is_child=is_child,
            owner_contact=_billing_owner_contact(payer) if is_child else None,
            current_plan=current_plan,
            sub_state=subscription_state(org),
            renewal_date=renewal_date,
            grace_end=(past_due_grace_end(payer).strftime('%d/%m/%Y') if past_due_grace_end(payer) else None),
            overage=plan_overage(org),
        )

    @app.route('/billing/checkout',methods=['POST'])
    @login_required
    @require_area('billing')
    @rate_limit(6,60)
    def billing_checkout():
        # Défense en profondeur : la facturation ne peut être pilotée que par le propriétaire.
        if current_role()!='OWNER':
            log_security_event('BILLING_CHECKOUT','FORBIDDEN',target='billing')
            abort(403)

        stripe=get_stripe()

        # Le plan doit toujours venir du formulaire et correspondre à un plan serveur connu.
        plan=(request.form.get('plan') or '').strip().upper()
        selected=STRIPE_PLANS.get(plan)

        if not stripe:
            flash("Le paiement en ligne sera bientôt disponible. Contactez-nous pour souscrire : votre essai en cours n'est pas interrompu.")
            return redirect(url_for('billing'))

        if not selected or not selected.get('price_id'):
            flash("Ce plan n'est pas disponible ou son tarif Stripe n'est pas configuré.")
            return redirect(url_for('billing'))

        org=current_org()
        user=current_user()

        # Organisation rattachée à une formule Multi-sociétés : l'abonnement est
        # porté par l'organisation mère, jamais souscrit une seconde fois ici.
        payer=billing_org(org)
        if payer and payer['id']!=org['id']:
            flash("Cette organisation est incluse dans l'abonnement Multi-sociétés de « "+payer['name']+" ». Gérez l'abonnement depuis cette organisation.")
            return redirect(url_for('billing'))

        # Verrou court anti-double-clic / requêtes répétées depuis le même navigateur.
        lock_key=f"billing_checkout_lock_{org['id']}"
        last_checkout=session.get(lock_key)
        now_ts=int(datetime.now(timezone.utc).timestamp())
        if last_checkout:
            try:
                if now_ts-int(last_checkout)<8:
                    flash("Une demande de paiement est déjà en cours. Patientez quelques secondes.")
                    return redirect(url_for('billing'))
            except (TypeError,ValueError):
                pass
        session[lock_key]=now_ts

        # Évite de créer plusieurs abonnements en parallèle pour la même organisation :
        # un abonnement existant se modifie dans le portail Stripe (changement de formule).
        if org['stripe_subscription_id'] and org['status'] in ('ACTIVE_PAID','PAST_DUE'):
            session.pop(lock_key,None)
            flash("Un abonnement Stripe existe déjà : changez de formule depuis le portail de facturation.")
            return _open_billing_portal(stripe,org)

        customer_id=org['stripe_customer_id']

        try:
            # Si un ancien Customer Stripe a été créé dans un autre mode
            # (test/live), il peut ne plus exister avec la clé actuellement utilisée.
            # Dans ce cas seulement, on recrée proprement le Customer.
            if customer_id:
                try:
                    stripe.Customer.retrieve(customer_id)
                except Exception as customer_error:
                    if 'No such customer' in str(customer_error):
                        current_app.logger.warning(
                            'Stripe customer not found; recreating customer org_id=%s',
                            org['id']
                        )
                        customer_id=None
                    else:
                        raise

            if not customer_id:
                customer=stripe.Customer.create(
                    email=user['email'],
                    name=org['name'],
                    metadata={'organization_id':str(org['id'])},
                    idempotency_key=f"profitos-customer-org-{org['id']}"
                )
                customer_id=customer['id']

                acx=auth_cx()
                try:
                    acx.execute(
                        'UPDATE organizations SET stripe_customer_id=?,updated_at=? WHERE id=?',
                        (customer_id,now(),org['id'])
                    )
                    acx.commit()
                finally:
                    acx.close()

            # Source de vérité Stripe : un abonnement actif/en essai/impayé existe
            # déjà pour ce client (ex. second paiement pendant l'essai) -> portail.
            if customer_id and _stripe_has_live_subscription(stripe,customer_id):
                session.pop(lock_key,None)
                log_security_event('BILLING_CHECKOUT','DUPLICATE_PREVENTED',organization_id=org['id'],target=f'plan:{plan}')
                flash("Un abonnement est déjà en place pour cette organisation : changez de formule depuis le portail de facturation.")
                return _open_billing_portal(stripe,org,customer_id)
            # Une seule session de paiement ouverte à la fois : les précédentes
            # sont expirées pour qu'elles ne puissent plus créer un 2e abonnement.
            if customer_id:
                _expire_open_checkout_sessions(stripe,customer_id)

            base=app_base_url().rstrip('/')

            subscription_data={
                'metadata':{
                    'organization_id':str(org['id']),
                    'plan':plan
                }
            }
            # Payer pendant l'essai ne fait pas perdre les jours restants
            # (Stripe exige une fin d'essai à plus de 48 h).
            trial_end_dt=parse_dt(org['trial_ends_at']) if trial_is_active(org) else None
            if trial_end_dt and trial_end_dt>datetime.now(timezone.utc)+timedelta(hours=49):
                subscription_data['trial_end']=int(trial_end_dt.timestamp())

            checkout=stripe.checkout.Session.create(
                customer=customer_id,
                mode='subscription',
                line_items=[
                    {
                        'price':selected['price_id'],
                        'quantity':1
                    }
                ],
                success_url=f'{base}{url_for("billing_success")}?session_id={{CHECKOUT_SESSION_ID}}',
                cancel_url=f'{base}{url_for("billing")}',
                client_reference_id=str(org['id']),
                metadata={
                    'organization_id':str(org['id']),
                    'plan':plan
                },
                subscription_data=subscription_data,
                # TVA : prix HT, TVA calculée par Stripe Tax selon l'adresse de facturation.
                automatic_tax={'enabled':STRIPE_AUTOMATIC_TAX},
                tax_id_collection={'enabled':True},
                billing_address_collection='required',
                customer_update={'address':'auto','name':'auto'},
                locale='fr',
                allow_promotion_codes=True,
                # Clé unique par tentative : les sessions précédentes viennent d'être
                # expirées ; une clé réutilisée rejouerait une session expirée.
                idempotency_key=f"profitos-checkout-{org['id']}-{plan}-{secrets.token_hex(8)}",
            )

        except Exception:
            session.pop(lock_key,None)
            current_app.logger.exception(
                'Stripe checkout failure request_id=%s org_id=%s plan=%s',
                getattr(g,'request_id','-'),
                org['id'],
                plan
            )
            log_security_event('BILLING_CHECKOUT','FAILURE',organization_id=org['id'],target=f'plan:{plan}')
            flash("Impossible d'ouvrir le paiement pour le moment. Réessayez plus tard.")
            return redirect(url_for('billing'))

        session.pop(lock_key,None)
        log_security_event('BILLING_CHECKOUT','SUCCESS',organization_id=org['id'],target=f'plan:{plan}')
        return redirect(checkout.url,code=303)

    @app.route('/billing/portal',methods=['POST'])
    @login_required
    @require_area('billing')
    @rate_limit(10,300)
    def billing_portal():
        if current_role()!='OWNER':
            log_security_event('BILLING_PORTAL','FORBIDDEN',target='billing')
            abort(403)

        stripe=get_stripe(); org=current_org()
        return _open_billing_portal(stripe,org)

    @app.route('/billing/success')
    @login_required
    def billing_success():
        # Ne jamais activer un plan depuis ce retour navigateur : seul le webhook signé fait foi.
        flash("Paiement reçu par Stripe. Votre abonnement sera affiché actif dès confirmation sécurisée.")
        return redirect(url_for('billing'))

    def _fetch_subscription(stripe, sub_id):
        """Relit l'abonnement depuis l'API Stripe (état courant, pas celui de
        l'événement). None si indisponible : l'appelant retombe sur l'événement."""
        if not stripe or not sub_id: return None
        try:
            return stripe.Subscription.retrieve(sub_id)
        except Exception:
            current_app.logger.warning('Stripe subscription re-fetch failed sub=%s',sub_id)
            return None

    def _plan_from_subscription(sub, org_row):
        """Plan déduit du price_id Stripe. Un price_id inconnu n'est jamais
        remplacé par metadata.plan (potentiellement obsolète) : on journalise et
        on conserve le plan actuel (retour None)."""
        items=((sub.get('items') or {}).get('data') or []) if sub else []
        price_id=((items[0].get('price') or {}).get('id')) if items else None
        plan=STRIPE_PRICE_TO_PLAN.get(price_id)
        if not plan and price_id:
            current_app.logger.warning(
                'Stripe subscription with unknown price_id=%s org_id=%s: plan unchanged',
                price_id, org_row['id'] if org_row else None
            )
        return plan

    @app.route('/billing/webhook',methods=['POST'])
    def billing_webhook():
        stripe=get_stripe()
        if not stripe: return ('billing disabled',503)

        # Les événements Stripe sont petits ; refuser un payload anormalement volumineux.
        if request.content_length and request.content_length>1024*1024:
            current_app.logger.warning('Stripe webhook payload too large request_id=%s',getattr(g,'request_id','-'))
            return ('payload too large',413)

        payload=request.get_data(cache=False); sig=request.headers.get('Stripe-Signature','')
        try:
            event=stripe.Webhook.construct_event(payload,sig,STRIPE_WEBHOOK_SECRET)
        except Exception:
            current_app.logger.warning('Stripe webhook signature rejected request_id=%s',getattr(g,'request_id','-'))
            return ('invalid signature',400)

        # Empêche de mélanger accidentellement événements Test et Live.
        expected_live=bool((STRIPE_SECRET_KEY or '').startswith('sk_live_'))
        if bool(event.get('livemode'))!=expected_live:
            current_app.logger.warning(
                'Stripe webhook mode mismatch request_id=%s event_id=%s',
                getattr(g,'request_id','-'),
                event.get('id')
            )
            return ('mode mismatch',400)

        event_id=event.get('id'); etype=event.get('type',''); obj=event['data']['object']
        acx=auth_cx()
        try:
            # Idempotence : Stripe peut livrer le même événement plusieurs fois.
            if event_id and acx.execute('SELECT id FROM stripe_webhook_events WHERE stripe_event_id=?',(event_id,)).fetchone():
                return ('',200)

            if etype=='checkout.session.completed':
                org_id=obj.get('metadata',{}).get('organization_id') or obj.get('client_reference_id')
                sub_id=obj.get('subscription'); plan=(obj.get('metadata',{}).get('plan') or '').upper()
                customer_id=obj.get('customer')

                # Prélèvement SEPA : la session peut être « complete » avant que le paiement soit
                # confirmé. On n'active alors rien ici ; customer.subscription.updated (active)
                # activera l'abonnement une fois le paiement réellement passé.
                payment_ok=obj.get('payment_status') in (None,'paid','no_payment_required')
                if org_id and sub_id and payment_ok:
                    org_row=acx.execute(
                        'SELECT * FROM organizations WHERE id=?',
                        (org_id,)
                    ).fetchone()

                    # N'accepte le Checkout que s'il appartient bien à l'organisation attendue.
                    if org_row and (not org_row['stripe_customer_id'] or org_row['stripe_customer_id']==customer_id):
                        stored_sub=org_row['stripe_subscription_id']
                        if stored_sub and stored_sub!=sub_id and org_row['status'] in ('ACTIVE_PAID','PAST_DUE'):
                            # Deuxième abonnement pour une organisation déjà abonnée : on
                            # ne bascule jamais dessus ; signalé pour remboursement manuel.
                            current_app.logger.error(
                                'Stripe duplicate subscription event_id=%s org_id=%s kept=%s duplicate=%s',
                                event_id, org_id, stored_sub, sub_id
                            )
                            log_security_event('BILLING_DUPLICATE_SUBSCRIPTION','FAILURE',organization_id=org_row['id'],target=f'subscription:{sub_id}')
                        else:
                            sub=_fetch_subscription(stripe,sub_id)
                            if sub:
                                # price_id inconnu : jamais de confiance dans metadata.plan
                                # (possiblement obsolète) ; on garde la formule actuelle.
                                plan=_plan_from_subscription(sub,org_row) or (org_row['plan'] if org_row['plan'] in STRIPE_PLANS else None)
                            else:
                                # API Stripe indisponible : plan de la session créée par notre serveur.
                                plan=plan if plan in STRIPE_PLANS else None
                            if not plan:
                                # Abonnement rattaché (les événements suivants le reconnaîtront)
                                # sans activer de formule indéterminée.
                                acx.execute('UPDATE organizations SET stripe_subscription_id=?,stripe_customer_id=COALESCE(stripe_customer_id,?),updated_at=? WHERE id=?',
                                            (sub_id,customer_id,now(),org_row['id']))
                            if plan:
                                acx.execute(
                                    "UPDATE organizations SET plan=?,status='ACTIVE_PAID',stripe_subscription_id=?,stripe_customer_id=COALESCE(stripe_customer_id,?),past_due_since=NULL,updated_at=? WHERE id=?",
                                    (plan,sub_id,customer_id,now(),org_row['id'])
                                )
                                sync_child_orgs(acx,org_row['id'])
                                reward_referrer_if_any(acx,org_row['id'])
                                apply_pending_referral_credit(acx,org_row['id'])
                    else:
                        current_app.logger.warning(
                            'Stripe checkout customer mismatch event_id=%s org_id=%s',
                            event_id,
                            org_id
                        )

            elif etype in ('customer.subscription.created','customer.subscription.updated','customer.subscription.deleted'):
                sub_id=obj.get('id'); metadata=obj.get('metadata') or {}
                row=acx.execute('SELECT * FROM organizations WHERE stripe_subscription_id=?',(sub_id,)).fetchone() if sub_id else None
                first_attachment=False
                if not row and sub_id and etype!='customer.subscription.deleted' and metadata.get('organization_id'):
                    # Seule exception à la règle « abonnement = celui enregistré » :
                    # la toute première association, pour le même client Stripe.
                    cand=acx.execute('SELECT * FROM organizations WHERE id=?',(metadata.get('organization_id'),)).fetchone()
                    if cand and not cand['stripe_subscription_id'] and (not cand['stripe_customer_id'] or cand['stripe_customer_id']==obj.get('customer')):
                        row=cand; first_attachment=True
                if not row:
                    current_app.logger.info('Stripe subscription event ignored (not the current subscription) event_id=%s sub=%s',event_id,sub_id)
                else:
                    # Statut frais depuis l'API (événements livrés dans le désordre).
                    sub=(_fetch_subscription(stripe,sub_id) if etype!='customer.subscription.deleted' else None) or obj
                    status=sub.get('status')
                    if etype=='customer.subscription.deleted' or status in ('canceled','unpaid','incomplete_expired'):
                        if not first_attachment:
                            acx.execute("UPDATE organizations SET status='CANCELED',updated_at=? WHERE id=?",(now(),row['id']))
                    elif status in ('active','trialing'):
                        plan=_plan_from_subscription(sub,row) or row['plan']
                        acx.execute("UPDATE organizations SET status='ACTIVE_PAID',plan=?,stripe_subscription_id=?,stripe_customer_id=COALESCE(stripe_customer_id,?),past_due_since=NULL,updated_at=? WHERE id=?",(plan,sub_id,obj.get('customer'),now(),row['id']))
                    elif status=='past_due':
                        # Seul un abonnement actif peut passer en impayé (jamais un abonnement résilié).
                        acx.execute("UPDATE organizations SET status='PAST_DUE',past_due_since=COALESCE(past_due_since,?),updated_at=? WHERE id=? AND status='ACTIVE_PAID'",(now(),now(),row['id']))
                    # 'incomplete' : premier paiement en cours -> aucun changement (jamais de rétrogradation).
                    sync_child_orgs(acx,row['id'])

            elif etype=='invoice.payment_failed':
                customer_id=obj.get('customer'); inv_sub=obj.get('subscription')
                if customer_id:
                    sql="SELECT id FROM organizations WHERE stripe_customer_id=? AND status='ACTIVE_PAID'"
                    params=[customer_id]
                    if inv_sub:
                        sql+=" AND stripe_subscription_id=?"; params.append(inv_sub)
                    for r in acx.execute(sql,tuple(params)).fetchall():
                        acx.execute("UPDATE organizations SET status='PAST_DUE',past_due_since=?,updated_at=? WHERE id=?",(now(),now(),r['id']))
                        sync_child_orgs(acx,r['id'])

            elif etype=='invoice.payment_succeeded':
                customer_id=obj.get('customer'); inv_sub=obj.get('subscription')
                if customer_id:
                    sql="SELECT id FROM organizations WHERE stripe_customer_id=? AND stripe_subscription_id IS NOT NULL AND status IN ('ACTIVE_PAID','PAST_DUE')"
                    params=[customer_id]
                    if inv_sub:
                        sql+=" AND stripe_subscription_id=?"; params.append(inv_sub)
                    for r in acx.execute(sql,tuple(params)).fetchall():
                        acx.execute("UPDATE organizations SET status='ACTIVE_PAID',past_due_since=NULL,updated_at=? WHERE id=?",(now(),r['id']))
                        sync_child_orgs(acx,r['id'])
                        apply_pending_referral_credit(acx,r['id'])

            if event_id:
                acx.execute('INSERT INTO stripe_webhook_events(stripe_event_id,event_type,processed_at) VALUES(?,?,?)',(event_id,etype,now()))
            acx.commit()
        except Exception:
            acx.rollback()
            current_app.logger.exception('Stripe webhook processing failure event=%s request_id=%s',etype,getattr(g,'request_id','-'))
            return ('webhook processing failed',500)
        finally:
            acx.close()
        return ('',200)


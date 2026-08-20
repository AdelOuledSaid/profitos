from profitos.runtime import *
from profitos.runtime import _token_user



def register(app):
    @app.route('/organizations/new',methods=['POST'])
    @login_required
    def org_new():
        name=request.form.get('org_name','').strip()
        if not name: flash("Nom d'organisation requis."); return redirect(url_for('settings'))
        c=auth_cx()
        slug=re.sub(r'[^a-z0-9]+','-',norm(name)).strip('-') or 'company'; base=slug; i=1
        while c.execute('SELECT id FROM organizations WHERE slug=?',(slug,)).fetchone(): i+=1; slug=f'{base}-{i}'
        trial=(datetime.now(timezone.utc)+timedelta(days=14)).isoformat()
        c.execute("INSERT INTO organizations(name,slug,plan,status,trial_ends_at,created_at,updated_at) VALUES(?,?,'TRIAL','ACTIVE',?,?,?)",(name,slug,trial,now(),now()))
        oid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
        c.execute("INSERT INTO memberships(user_id,organization_id,role,created_at) VALUES(?,?,'OWNER',?)",(session['user_id'],oid,now())); c.commit(); c.close()
        session['org_id']=oid; session['role']='OWNER'; init_tenant_db()
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
        session['org_id']=org_id; session['role']=m['role']; init_tenant_db()
        log_activity('ORG_SWITCH','Changement d\'organisation active')
        return redirect(url_for('home'))

    @app.route('/signup',methods=['GET','POST'])
    @rate_limit(10,900)
    def signup():
        if session.get('user_id'): return redirect(url_for('home'))
        if request.method=='POST':
            name=request.form.get('full_name','').strip(); email=request.form.get('email','').strip().lower(); pw=request.form.get('password',''); company=request.form.get('company_name','').strip()
            if not name or not email or not pw or not company: flash('Tous les champs sont obligatoires.'); return redirect(request.url)
            err=password_error(pw)
            if err: flash(err); return redirect(request.url)
            c=auth_cx()
            if c.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone(): c.close(); flash('Un compte existe déjà avec cet e-mail.'); return redirect(url_for('login'))
            slug=re.sub(r'[^a-z0-9]+','-',norm(company)).strip('-') or 'company'; base=slug; i=1
            while c.execute('SELECT id FROM organizations WHERE slug=?',(slug,)).fetchone(): i+=1; slug=f'{base}-{i}'
            from datetime import timedelta
            trial=(datetime.now(timezone.utc)+timedelta(days=14)).isoformat()
            c.execute("INSERT INTO organizations(name,slug,plan,status,trial_ends_at,created_at,updated_at) VALUES(?,?,'TRIAL','ACTIVE',?,?,?)",(company,slug,trial,now(),now())); oid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.execute('INSERT INTO users(email,password_hash,full_name,is_active,created_at,updated_at) VALUES(?,?,?,1,?,?)',(email,generate_password_hash(pw),name,now(),now())); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.execute("INSERT INTO memberships(user_id,organization_id,role,created_at) VALUES(?,?,'OWNER',?)",(uid,oid,now())); c.commit()
            user=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close()
            session.clear(); session.permanent=True; session['user_id']=uid; session['org_id']=oid; session['role']='OWNER'; session['auth_version']=int(user.get('auth_version') or 0); init_tenant_db()
            tc=cx(); tc.execute("INSERT OR IGNORE INTO app_settings(id,onboarding_complete,currency,locale,notifications_enabled,created_at,updated_at) VALUES(1,0,'EUR','fr-FR',1,?,?)",(now(),now())); tc.commit(); tc.close()
            log_activity('SIGNUP',f'Création organisation {company}')
            result=send_verification_email(user)
            if result.get('sent'):
                flash('Compte créé. Un email de confirmation a été envoyé.')
            elif result.get('dry_run'):
                flash("Compte créé. Vérification email : service email non configuré, aucun email envoyé (mode simulation).")
            else:
                current_app.logger.error('Verification email failed after signup: %s', result.get('error','unknown error'))
                flash("Compte créé, mais l'email de confirmation n'a pas pu être envoyé. Utilisez « Renvoyer l'email de vérification » dans votre compte.")
            return redirect(url_for('onboarding'))
        return render_template('signup.html')

    @app.route('/login',methods=['GET','POST'])
    @rate_limit(8,300)
    def login():
        if session.get('user_id'): return redirect(url_for('home'))
        if request.method=='POST':
            email=request.form.get('email','').strip().lower(); pw=request.form.get('password','')
            c=auth_cx(); u=c.execute('SELECT * FROM users WHERE email=? AND is_active=1',(email,)).fetchone()
            if not u or not check_password_hash(u['password_hash'],pw):
                failed_uid=u['id'] if u else None
                failed_org=None
                if u:
                    fm=c.execute('SELECT organization_id FROM memberships WHERE user_id=? ORDER BY id LIMIT 1',(u['id'],)).fetchone()
                    failed_org=fm['organization_id'] if fm else None
                c.close()
                log_security_event('LOGIN_FAILED','FAILURE',user_id=failed_uid,organization_id=failed_org,target='account')
                flash('E-mail ou mot de passe incorrect.')
                return redirect(request.url)
            m=c.execute('SELECT * FROM memberships WHERE user_id=? ORDER BY id LIMIT 1',(u['id'],)).fetchone(); c.close()
            if not m: flash('Aucune organisation associée.'); return redirect(request.url)
            session.clear(); session.permanent=True; session['user_id']=u['id']; session['org_id']=m['organization_id']; session['role']=m['role']; session['auth_version']=int(u.get('auth_version') or 0); init_tenant_db(); log_activity('LOGIN','Connexion'); log_security_event('LOGIN_SUCCESS','SUCCESS',user_id=u['id'],organization_id=m['organization_id'],target='account')
            return redirect(safe_next_url(request.args.get('next')) or url_for('home'))
        return render_template('login.html')

    @app.route('/logout',methods=['POST'])
    @login_required
    def logout():
        if session.get('user_id'):
            log_activity('LOGOUT','Déconnexion')
            log_security_event('LOGOUT','SUCCESS',target='account')
        session.clear(); return redirect(url_for('login'))

    @app.route('/verify/<token>')
    @rate_limit(20,900)
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
        flash('Adresse email vérifiée. Merci !')
        return redirect(url_for('home') if session.get('user_id') else url_for('login'))

    @app.route('/verify/resend',methods=['POST'])
    @login_required
    @rate_limit(3,900)
    def verify_resend():
        u=current_user()
        if u['email_verified']:
            flash('Votre email est déjà vérifié.')
        else:
            result=send_verification_email(u)
            if result.get('sent'):
                flash("Email de vérification renvoyé.")
            elif result.get('dry_run'):
                flash("Service email non configuré — email non envoyé (mode simulation).")
            else:
                flash("L'email n'a pas pu être envoyé pour le moment. Réessayez dans quelques minutes.")
        return redirect(request.referrer or url_for('home'))

    @app.route('/forgot-password',methods=['GET','POST'])
    @rate_limit(5,900)
    def forgot_password():
        if request.method=='POST':
            email=request.form.get('email','').strip().lower()
            c=auth_cx(); u=c.execute('SELECT * FROM users WHERE email=? AND is_active=1',(email,)).fetchone(); c.close()
            if u:
                send_reset_email(u)
                log_security_event('PASSWORD_RESET_REQUEST','ACCEPTED',user_id=u['id'],target='account')
            else:
                log_security_event('PASSWORD_RESET_REQUEST','ACCEPTED',target='unknown_account')
            # Message volontairement identique que le compte existe ou non, pour ne pas révéler les emails inscrits.
            flash("Si un compte existe avec cet email, un lien de réinitialisation vient d'être envoyé.")
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
            err=password_error(pw)
            if err:
                flash(err); return redirect(request.url)
            if pw!=pw2:
                flash('Les deux mots de passe ne correspondent pas.'); return redirect(request.url)

            # Consommation atomique + rotation de auth_version :
            # 1) le lien devient immédiatement inutilisable ;
            # 2) toutes les anciennes sessions de ce compte sont invalidées.
            c=auth_cx()
            result=c.execute(
                'UPDATE users SET password_hash=?,reset_token=NULL,reset_token_expires=NULL,'
                'auth_version=COALESCE(auth_version,0)+1,updated_at=? '
                'WHERE id=? AND reset_token=?',
                (generate_password_hash(pw),now(),u['id'],stored_token)
            )
            c.commit(); c.close()
            if getattr(result,'rowcount',0)!=1:
                flash("Ce lien de réinitialisation a déjà été utilisé.")
                return redirect(url_for('forgot_password'))

            log_security_event('PASSWORD_RESET','SUCCESS',user_id=u['id'],target='account')
            # La session courante, si elle existe, ne doit pas survivre au changement.
            session.clear()
            flash('Mot de passe mis à jour. Toutes les anciennes sessions ont été invalidées. Vous pouvez vous reconnecter.')
            return redirect(url_for('login'))

        return render_template('reset_password.html',token=token)

    @app.route('/onboarding',methods=['GET','POST'])
    @login_required
    def onboarding():
        c=cx(); p=c.execute('SELECT * FROM company WHERE id=1').fetchone(); c.close(); org=current_org()
        if request.method=='POST':
            company=request.form.get('company_name','').strip(); city=request.form.get('city','').strip(); dep=request.form.get('department','').strip(); allowed=request.form.get('allowed_departments','').strip() or dep; activities=request.form.get('activities','').strip(); certs=request.form.get('certifications','').strip()
            c=cx(); c.execute("INSERT INTO company(id,name,city,department,allowed_departments,activities,certifications,updated_at) VALUES(1,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,city=excluded.city,department=excluded.department,allowed_departments=excluded.allowed_departments,activities=excluded.activities,certifications=excluded.certifications,updated_at=excluded.updated_at",(company,city,dep,allowed,activities,certs,now())); c.execute("INSERT INTO app_settings(id,onboarding_complete,currency,locale,notifications_enabled,created_at,updated_at) VALUES(1,1,'EUR','fr-FR',1,?,?) ON CONFLICT(id) DO UPDATE SET onboarding_complete=1,updated_at=excluded.updated_at",(now(),now())); c.commit(); c.close(); log_activity('ONBOARDING_COMPLETE','Profil entreprise complété'); flash('Onboarding terminé.'); return redirect(url_for('home'))
        return render_template('onboarding.html',profile=p,org=org)

    @app.route('/settings',methods=['GET','POST'])
    @login_required
    @require_area('settings')
    def settings():
        if request.method=='POST':
            currency=request.form.get('currency','EUR'); locale=request.form.get('locale','fr-FR'); notif=1 if request.form.get('notifications_enabled')=='on' else 0
            slack_url=request.form.get('slack_webhook_url','').strip() or None; teams_url=request.form.get('teams_webhook_url','').strip() or None
            if slack_url and not validate_webhook_url(slack_url): flash('Webhook Slack invalide ou non autorisé.'); return redirect(request.url)
            if teams_url and not validate_webhook_url(teams_url): flash('Webhook Teams invalide ou non autorisé.'); return redirect(request.url)
            c=cx(); c.execute("INSERT INTO app_settings(id,onboarding_complete,currency,locale,notifications_enabled,slack_webhook_url,teams_webhook_url,created_at,updated_at) VALUES(1,1,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET currency=excluded.currency,locale=excluded.locale,notifications_enabled=excluded.notifications_enabled,slack_webhook_url=excluded.slack_webhook_url,teams_webhook_url=excluded.teams_webhook_url,updated_at=excluded.updated_at",(currency,locale,notif,slack_url,teams_url,now(),now())); c.commit(); c.close(); log_activity('SETTINGS_UPDATE','Paramètres mis à jour'); flash('Paramètres enregistrés.'); return redirect(url_for('settings'))
        c=cx(); s=c.execute('SELECT * FROM app_settings WHERE id=1').fetchone(); c.close(); ac=auth_cx(); activity=ac.execute('SELECT * FROM activity_log WHERE organization_id=? ORDER BY id DESC LIMIT 30',(session['org_id'],)).fetchall(); ac.close(); return render_template('settings.html',app_settings=s,org=current_org(),user=current_user(),activity=activity)

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
        finally:
            c.close()
        return render_template('security_events.html',events=rows)

    @app.route('/settings/test-notification',methods=['POST'])
    @login_required
    def test_notification():
        notify_org(f"🔔 Test ProfitOS depuis {current_org()['name']} — les notifications fonctionnent.")
        flash("Notification de test envoyée (si un webhook est configuré et joignable).")
        return redirect(url_for('settings'))

    @app.route('/team')
    @login_required
    @require_area('team')
    def team():
        c=auth_cx(); rows=c.execute('SELECT memberships.*,users.email,users.full_name FROM memberships JOIN users ON users.id=memberships.user_id WHERE memberships.organization_id=? ORDER BY memberships.id',(session['org_id'],)).fetchall(); c.close(); return render_template('team.html',members=rows)

    def send_invite_email(user, org, role, dry_run=None):
        token=gen_token()
        expires=(datetime.now(timezone.utc)+timedelta(days=7)).isoformat()
        c=auth_cx()
        c.execute('UPDATE users SET reset_token=?,reset_token_expires=? WHERE id=?',(token_digest(token),expires,user['id']))
        c.commit(); c.close()
        base=os.environ.get('APP_BASE_URL','http://127.0.0.1:5050')
        link=f"{base}{url_for('reset_password',token=token)}"
        html=render_template('email_transactional.html',title='Vous avez été invité(e) sur ProfitOS',
            intro=f"{org['name']} vous a invité(e) à rejoindre ProfitOS avec le rôle \"{ROLE_LABELS.get(role,role)}\". Cliquez ci-dessous pour définir votre mot de passe et accéder à votre compte.",
            cta_label='Définir mon mot de passe',cta_url=link,footer="Si vous ne connaissez pas cette organisation, vous pouvez ignorer cet email.")
        return send_email(user['email'],f"Invitation à rejoindre {org['name']} sur ProfitOS",html,dry_run=dry_run)

    @app.route('/team/invite',methods=['POST'])
    @login_required
    @require_area('team')
    @rate_limit(10,3600)
    def team_invite():
        if current_role()!='OWNER':
            flash("Seul le propriétaire peut inviter des membres."); return redirect(url_for('team'))
        email=request.form.get('email','').strip().lower(); role=request.form.get('role','MEMBER').upper()
        if role not in ROLES: role='MEMBER'
        if not email: flash('Email requis.'); return redirect(url_for('team'))
        c=auth_cx()
        already=c.execute('SELECT 1 FROM memberships JOIN users ON users.id=memberships.user_id WHERE users.email=? AND memberships.organization_id=?',(email,session['org_id'])).fetchone()
        if already: c.close(); flash('Cette personne est déjà membre de cette organisation.'); return redirect(url_for('team'))
        user=c.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
        if not user:
            c.execute('INSERT INTO users(email,password_hash,full_name,is_active,created_at,updated_at) VALUES(?,?,?,1,?,?)',
                (email,generate_password_hash(secrets.token_hex(16)),'',now(),now()))
            uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
            user=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
        c.execute("INSERT INTO memberships(user_id,organization_id,role,created_at) VALUES(?,?,?,?)",(user['id'],session['org_id'],role,now())); c.commit(); c.close()
        org=current_org(); result=send_invite_email(user,org,role)
        log_activity('TEAM_INVITE',f'Invitation envoyée à {email} ({ROLE_LABELS.get(role,role)})')
        log_security_event('TEAM_INVITE','SUCCESS',user_id=session.get('user_id'),target=f'user:{user["id"]}')
        if result.get('sent'):
            flash(f"Invitation envoyée à {email}.")
        elif result.get('dry_run'):
            flash(f"Membre ajouté. Service email non configuré — email d'invitation non envoyé (mode simulation) à {email}.")
        else:
            flash(f"Membre ajouté, mais l'email d'invitation n'a pas pu être envoyé à {email}.")
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
        c=auth_cx(); c.execute('DELETE FROM memberships WHERE user_id=? AND organization_id=?',(uid,session['org_id'])); c.commit(); c.close()
        log_activity('TEAM_REMOVE',f'Membre #{uid} retiré')
        log_security_event('TEAM_REMOVE','SUCCESS',target=f'user:{uid}')
        flash('Membre retiré de l\'organisation.'); return redirect(url_for('team'))

    @app.route('/billing')
    @login_required
    @require_area('billing')
    def billing():
        org=current_org()
        plans={k:v for k,v in STRIPE_PLANS.items() if v.get('price_id')}

        cancellation_scheduled=False
        cancellation_date=None

        stripe=get_stripe()
        sub_id=org['stripe_subscription_id'] if org else None

        if stripe and sub_id and org['status'] in ('ACTIVE_PAID','PAST_DUE'):
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

                if cancellation_scheduled:
                    if cancellation_date:
                        flash(
                            f"Annulation programmée le {cancellation_date}. "
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
            flash("La facturation Stripe n'est pas configurée.")
            return redirect(url_for('billing'))

        if not selected or not selected.get('price_id'):
            flash("Ce plan n'est pas disponible ou son tarif Stripe n'est pas configuré.")
            return redirect(url_for('billing'))

        org=current_org()
        user=current_user()

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

        # Évite de créer plusieurs abonnements en parallèle pour la même organisation.
        if org['stripe_subscription_id'] and org['status'] in ('ACTIVE_PAID','PAST_DUE'):
            session.pop(lock_key,None)
            flash("Un abonnement Stripe existe déjà. Utilisez le portail pour le modifier ou le gérer.")
            return redirect(url_for('billing'))

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

            base=os.environ.get('APP_BASE_URL','https://app.profitos.fr').rstrip('/')

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
                subscription_data={
                    'metadata':{
                        'organization_id':str(org['id']),
                        'plan':plan
                    }
                },
                idempotency_key=f"profitos-checkout-{org['id']}-{plan}-{now_ts//300}",
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
        if not stripe or not org['stripe_customer_id']:
            flash("Aucun abonnement Stripe associé à cette organisation.")
            return redirect(url_for('billing'))
        base=os.environ.get('APP_BASE_URL','http://127.0.0.1:5050').rstrip('/')
        try:
            portal=stripe.billing_portal.Session.create(customer=org['stripe_customer_id'],return_url=f'{base}{url_for("billing")}')
        except Exception:
            current_app.logger.exception('Stripe portal failure request_id=%s org_id=%s',getattr(g,'request_id','-'),org['id'])
            log_security_event('BILLING_PORTAL','FAILURE',organization_id=org['id'],target='billing')
            flash("Impossible d'ouvrir le portail de facturation pour le moment.")
            return redirect(url_for('billing'))
        log_security_event('BILLING_PORTAL','SUCCESS',organization_id=org['id'],target='billing')
        return redirect(portal.url,code=303)

    @app.route('/billing/success')
    @login_required
    def billing_success():
        # Ne jamais activer un plan depuis ce retour navigateur : seul le webhook signé fait foi.
        flash("Paiement reçu par Stripe. Votre abonnement sera affiché actif dès confirmation sécurisée.")
        return redirect(url_for('billing'))

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

                if org_id and sub_id and plan in STRIPE_PLANS:
                    org_row=acx.execute(
                        'SELECT id,stripe_customer_id FROM organizations WHERE id=?',
                        (org_id,)
                    ).fetchone()

                    # N'accepte le Checkout que s'il appartient bien à l'organisation attendue.
                    if org_row and (not org_row['stripe_customer_id'] or org_row['stripe_customer_id']==customer_id):
                        acx.execute(
                            "UPDATE organizations SET plan=?,status='ACTIVE_PAID',stripe_subscription_id=?,updated_at=? WHERE id=?",
                            (plan,sub_id,now(),org_id)
                        )
                    else:
                        current_app.logger.warning(
                            'Stripe checkout customer mismatch event_id=%s org_id=%s',
                            event_id,
                            org_id
                        )

            elif etype in ('customer.subscription.created','customer.subscription.updated','customer.subscription.deleted'):
                sub_id=obj.get('id'); status=obj.get('status'); metadata=obj.get('metadata') or {}
                org_id=metadata.get('organization_id')
                row=acx.execute('SELECT id FROM organizations WHERE stripe_subscription_id=?',(sub_id,)).fetchone() if sub_id else None
                if not row and org_id:
                    row=acx.execute('SELECT id FROM organizations WHERE id=?',(org_id,)).fetchone()
                if row:
                    price_id=None
                    items=((obj.get('items') or {}).get('data') or [])
                    if items:
                        price_id=((items[0].get('price') or {}).get('id'))
                    plan=STRIPE_PRICE_TO_PLAN.get(price_id) or (metadata.get('plan') or '').upper()
                    if plan not in STRIPE_PLANS: plan=None
                    if etype=='customer.subscription.deleted' or status in ('canceled','unpaid','incomplete_expired'):
                        acx.execute("UPDATE organizations SET status='CANCELED',updated_at=? WHERE id=?",(now(),row['id']))
                    elif status in ('active','trialing'):
                        if plan:
                            acx.execute("UPDATE organizations SET status='ACTIVE_PAID',plan=?,stripe_subscription_id=?,updated_at=? WHERE id=?",(plan,sub_id,now(),row['id']))
                        else:
                            acx.execute("UPDATE organizations SET status='ACTIVE_PAID',stripe_subscription_id=?,updated_at=? WHERE id=?",(sub_id,now(),row['id']))
                    elif status in ('past_due','incomplete'):
                        acx.execute("UPDATE organizations SET status='PAST_DUE',stripe_subscription_id=?,updated_at=? WHERE id=?",(sub_id,now(),row['id']))

            elif etype=='invoice.payment_failed':
                customer_id=obj.get('customer')
                if customer_id:
                    acx.execute("UPDATE organizations SET status='PAST_DUE',updated_at=? WHERE stripe_customer_id=?",(now(),customer_id))

            elif etype=='invoice.payment_succeeded':
                customer_id=obj.get('customer')
                if customer_id:
                    acx.execute("UPDATE organizations SET status='ACTIVE_PAID',updated_at=? WHERE stripe_customer_id=? AND stripe_subscription_id IS NOT NULL",(now(),customer_id))

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


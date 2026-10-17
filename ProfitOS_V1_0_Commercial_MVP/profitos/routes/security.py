"""Routes de sécurité du compte : double authentification, changement de mot de
passe / d'e-mail, invitations d'équipe, export personnel RGPD, organisation en
cours de suppression. Les garde-fous globaux (2FA obligatoire, organisation
supprimée) sont enregistrés ici comme hooks before_request."""
import io
import json

from profitos.runtime import *
from profitos import account_security as acsec
from profitos import totp as totp_mod
from profitos.accounting import paris_today

_GUARD_ALLOWED = {
    'static', 'logout', 'login', 'login_two_factor', 'org_switch', 'two_factor_setup', 'account_security',
    'org_deletion_pending', 'org_deletion_cancel', 'personal_data_export', 'healthz', 'readyz', 'ops_health',
    'verify_email', 'verify_resend', 'team_invitation', 'account_email_confirm',
}


def _invitation_by_token(token):
    c = auth_cx()
    try:
        inv = c.execute('SELECT * FROM team_invitations WHERE token_hash=?', (token_digest(token),)).fetchone()
        if not inv or inv['accepted_at'] or inv['revoked_at'] or (inv['expires_at'] or '') <= now():
            return None, None
        org = c.execute('SELECT id,name,deleted_at FROM organizations WHERE id=?', (inv['organization_id'],)).fetchone()
        if not org or org['deleted_at']:
            return None, None
        return inv, org
    finally:
        c.close()


def _accept_invitation(c, inv, user_id):
    """Crée l'adhésion (et seulement à ce moment-là) puis consomme l'invitation."""
    r = c.execute('UPDATE team_invitations SET accepted_at=?,accepted_by=? WHERE id=? AND accepted_at IS NULL AND revoked_at IS NULL',
                  (now(), user_id, inv['id']))
    if getattr(r, 'rowcount', 0) != 1:
        return False
    if not c.execute('SELECT 1 FROM memberships WHERE user_id=? AND organization_id=?',
                     (user_id, inv['organization_id'])).fetchone():
        c.execute('INSERT INTO memberships(user_id,organization_id,role,created_at) VALUES(?,?,?,?)',
                  (user_id, inv['organization_id'], inv['role'], now()))
    c.execute('UPDATE users SET membership_lost_at=NULL WHERE id=?', (user_id,))
    return True


def register(app):
    @app.before_request
    def account_security_guards():
        uid = session.get('user_id'); oid = session.get('org_id')
        if not uid or not oid or request.endpoint in _GUARD_ALLOWED or request.path.startswith('/api/v1/'):
            return
        c = auth_cx()
        try:
            org = c.execute('SELECT deleted_at,require_2fa FROM organizations WHERE id=?', (oid,)).fetchone()
            user = c.execute('SELECT totp_enabled,totp_secret FROM users WHERE id=?', (uid,)).fetchone()
        finally:
            c.close()
        if org and org['deleted_at']:
            return redirect(url_for('org_deletion_pending'))
        if org and acsec.org_requires_2fa(org) and user and not acsec.user_has_2fa(user):
            flash("Votre organisation exige la double authentification : activez-la pour continuer.")
            return redirect(url_for('two_factor_setup'))

    @app.after_request
    def account_security_cookies(response):
        return acsec.set_device_cookie(response)

    # ------------------------------------------------------------------ 2FA à la connexion
    @app.route('/login/2fa', methods=['GET', 'POST'])
    @rate_limit(10, 300)
    def login_two_factor():
        pending = session.get('pending_2fa') or {}
        started = parse_dt(pending.get('at')) if pending else None
        if not pending or not started or (datetime.now(timezone.utc) - started).total_seconds() > 300:
            session.pop('pending_2fa', None)
            flash('Session de connexion expirée. Veuillez vous reconnecter.')
            return redirect(url_for('login'))
        if request.method == 'POST':
            c = auth_cx()
            user = c.execute('SELECT * FROM users WHERE id=? AND is_active=1', (pending['uid'],)).fetchone()
            m = c.execute('SELECT * FROM memberships WHERE user_id=? AND organization_id=?',
                          (pending['uid'], pending['org'])).fetchone()
            c.close()
            if not user or not m:
                session.pop('pending_2fa', None)
                return redirect(url_for('login'))
            if acsec.account_locked_until(user):
                session.pop('pending_2fa', None)
                flash('Trop de tentatives infructueuses : la connexion à ce compte est temporairement bloquée. Réessayez dans quelques minutes.')
                return redirect(url_for('login'))
            method = acsec.verify_second_factor(user, request.form.get('code', ''))
            if not method:
                pending['fails'] = int(pending.get('fails', 0)) + 1
                log_security_event('LOGIN_2FA_FAILED', 'FAILURE', user_id=user['id'], target='account')
                # Compteur côté serveur : le compteur de la session (cookie) peut être rejoué,
                # celui du compte verrouille réellement après le seuil.
                if acsec.register_login_failure(user):
                    log_security_event('ACCOUNT_LOCKED', 'BLOCKED', user_id=user['id'], target='2fa')
                if pending['fails'] >= 5:
                    session.pop('pending_2fa', None)
                    flash('Trop de codes incorrects. Veuillez vous reconnecter.')
                    return redirect(url_for('login'))
                session['pending_2fa'] = pending
                flash('Code incorrect.')
                return redirect(url_for('login_two_factor'))
            next_url = safe_next_url(pending.get('next'))
            acsec.reset_login_failures(user['id'])
            acsec.start_user_session(user, m['organization_id'], m['role'])
            init_tenant_db()
            log_activity('LOGIN', 'Connexion (double authentification)')
            log_security_event('LOGIN_SUCCESS', 'SUCCESS', user_id=user['id'], organization_id=m['organization_id'],
                               target='2fa:' + method)
            if method == 'recovery':
                flash("Vous vous êtes connecté avec un code de secours : il n'est plus utilisable. Pensez à en générer de nouveaux.")
            return redirect(next_url or url_for('home'))
        return render_template('login_2fa.html')

    # ------------------------------------------------------------------ page Sécurité
    @app.route('/compte/securite')
    @login_required
    def account_security():
        user = current_user()
        c = auth_cx()
        sessions = c.execute('SELECT user_agent,created_at,last_seen_at FROM user_sessions WHERE user_id=? AND revoked_at IS NULL '
                             'ORDER BY id DESC LIMIT 10', (user['id'],)).fetchall()
        org = c.execute('SELECT require_2fa FROM organizations WHERE id=?', (session['org_id'],)).fetchone()
        c.close()
        recovery_left = len(json.loads(user.get('totp_recovery_codes') or '[]')) if acsec.user_has_2fa(user) else 0
        return render_template('account_security.html', user=user, has_2fa=acsec.user_has_2fa(user),
                               recovery_left=recovery_left, sessions=sessions,
                               org_requires_2fa=acsec.org_requires_2fa(org), is_owner=current_role() == 'OWNER')

    @app.route('/compte/nom', methods=['POST'])
    @login_required
    def account_name_change():
        """Prénom et nom affichés dans l'application (accueil, équipe, e-mails d'invitation)."""
        first = ' '.join(request.form.get('first_name', '').split())[:60]
        last = ' '.join(request.form.get('last_name', '').split())[:60]
        if not first or not last:
            flash('Indiquez votre prénom et votre nom.')
            return redirect(url_for('account_security'))
        c = auth_cx()
        c.execute('UPDATE users SET full_name=?,updated_at=? WHERE id=?', (f'{first} {last}', now(), session['user_id']))
        c.commit(); c.close()
        log_activity('ACCOUNT_NAME_CHANGED', 'Nom du compte modifié')
        flash('Votre nom a été mis à jour.')
        return redirect(url_for('account_security'))

    @app.route('/compte/mot-de-passe', methods=['POST'])
    @login_required
    @rate_limit(10, 900)
    def account_password_change():
        user = current_user()
        if not acsec.verify_reauth(user):
            flash('Mot de passe actuel incorrect.')
            return redirect(url_for('account_security'))
        pw = request.form.get('new_password', ''); pw2 = request.form.get('new_password_confirm', '')
        err = password_error(pw, user['email'])
        if err:
            flash(err); return redirect(url_for('account_security'))
        if pw != pw2:
            flash('Les deux mots de passe ne correspondent pas.'); return redirect(url_for('account_security'))
        c = auth_cx()
        c.execute('UPDATE users SET password_hash=?,password_changed_at=?,updated_at=? WHERE id=?',
                  (generate_password_hash(pw), now(), now(), user['id']))
        c.commit(); c.close()
        # Toutes les autres sessions (autres appareils) sont fermées ; celle-ci reste ouverte.
        acsec.bump_auth_version_keep_current(user['id'])
        log_security_event('PASSWORD_CHANGED', 'SUCCESS', target='account')
        acsec.notify_user(user, 'Mot de passe modifié',
                          "Le mot de passe de votre compte ProfitOS vient d'être modifié et vos autres appareils ont été déconnectés. "
                          "Si vous n'êtes pas à l'origine de ce changement, réinitialisez-le immédiatement.",
                          cta_label='Réinitialiser mon mot de passe', cta_endpoint='forgot_password')
        flash('Mot de passe modifié. Vos autres appareils ont été déconnectés.')
        return redirect(url_for('account_security'))

    @app.route('/compte/email', methods=['POST'])
    @login_required
    @rate_limit(5, 900)
    def account_email_change():
        user = current_user()
        if not acsec.verify_reauth(user):
            flash('Mot de passe actuel incorrect.')
            return redirect(url_for('account_security'))
        new_email = request.form.get('new_email', '').strip().lower()
        if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', new_email) or len(new_email) > 254:
            flash('Adresse e-mail invalide.'); return redirect(url_for('account_security'))
        if new_email == (user['email'] or '').lower():
            flash("C'est déjà votre adresse actuelle."); return redirect(url_for('account_security'))
        c = auth_cx()
        taken = c.execute('SELECT 1 FROM users WHERE email=?', (new_email,)).fetchone()
        token = gen_token()
        if not taken:
            c.execute('UPDATE users SET pending_email=?,pending_email_token=?,pending_email_sent_at=? WHERE id=?',
                      (new_email, token_digest(token), now(), user['id']))
            c.commit()
        c.close()
        if not taken:
            link = f"{app_base_url()}{url_for('account_email_confirm', token=token)}"
            html = render_template('email_transactional.html', title='Confirmez votre nouvelle adresse e-mail',
                                   intro="Cliquez ci-dessous pour confirmer cette adresse comme nouvel identifiant de votre compte ProfitOS. Ce lien est valable 24 heures.",
                                   cta_label='Confirmer cette adresse', cta_url=link,
                                   footer="Si vous n'êtes pas à l'origine de cette demande, ignorez ce message.")
            send_email(new_email, 'Confirmez votre nouvelle adresse e-mail ProfitOS', html, kind='system')
            acsec.notify_user(user, "Changement d'adresse e-mail demandé",
                              "Une demande de changement de l'adresse e-mail de votre compte ProfitOS a été faite. "
                              "Elle ne prendra effet qu'après confirmation depuis la nouvelle adresse. "
                              "Si vous n'êtes pas à l'origine de cette demande, changez votre mot de passe.",
                              cta_label='Sécuriser mon compte', cta_endpoint='forgot_password')
        log_security_event('EMAIL_CHANGE_REQUESTED', 'ACCEPTED', target='account')
        # Message identique que l'adresse soit libre ou non (pas d'énumération).
        flash('Un lien de confirmation a été envoyé à la nouvelle adresse. Le changement prendra effet après confirmation.')
        return redirect(url_for('account_security'))

    @app.route('/compte/email/confirmer/<token>')
    @limiter.limit('20 per 900 seconds', override_defaults=True)
    def account_email_confirm(token):
        c = auth_cx()
        u = c.execute('SELECT * FROM users WHERE pending_email_token=?', (token_digest(token),)).fetchone()
        sent = parse_dt(u['pending_email_sent_at']) if u else None
        if not u or not u['pending_email'] or not sent or (datetime.now(timezone.utc) - sent).total_seconds() > 86400:
            c.close()
            flash('Lien de confirmation invalide ou expiré.')
            return redirect(url_for('login'))
        if c.execute('SELECT 1 FROM users WHERE email=? AND id!=?', (u['pending_email'], u['id'])).fetchone():
            c.execute('UPDATE users SET pending_email=NULL,pending_email_token=NULL WHERE id=?', (u['id'],))
            c.commit(); c.close()
            flash("Cette adresse est déjà utilisée par un autre compte.")
            return redirect(url_for('login'))
        old_email = u['email']
        c.execute('UPDATE users SET email=?,email_verified=1,pending_email=NULL,pending_email_token=NULL,'
                  'pending_email_sent_at=NULL,updated_at=? WHERE id=?', (u['pending_email'], now(), u['id']))
        c.commit(); c.close()
        log_security_event('EMAIL_CHANGED', 'SUCCESS', user_id=u['id'], target='account')
        acsec.notify_user({'id': u['id'], 'email': old_email}, 'Adresse e-mail modifiée',
                          "L'adresse e-mail de votre compte ProfitOS a été modifiée. Si vous n'êtes pas à l'origine de ce changement, contactez immédiatement le support.")
        flash('Nouvelle adresse e-mail confirmée.')
        return redirect(url_for('account_security') if session.get('user_id') == u['id'] else url_for('login'))

    # ------------------------------------------------------------------ 2FA : activation / désactivation
    @app.route('/compte/2fa', methods=['GET', 'POST'])
    @login_required
    def two_factor_setup():
        user = current_user()
        if acsec.user_has_2fa(user):
            return redirect(url_for('account_security'))
        c = auth_cx()
        secret = None
        if user.get('totp_secret'):
            try:
                secret = acsec.totp_secret_for(user)
            except ValueError:
                secret = None
        if not secret:
            secret = totp_mod.new_secret()
            c.execute('UPDATE users SET totp_secret=?,totp_enabled=0,totp_last_step=NULL WHERE id=?',
                      (acsec.encrypt_totp_secret(user, secret), user['id']))
            c.commit()
        if request.method == 'POST':
            step = totp_mod.verify(secret, request.form.get('code', ''))
            if step is None:
                c.close()
                flash('Code incorrect : vérifiez l\'heure de votre téléphone et réessayez.')
                return redirect(url_for('two_factor_setup'))
            codes = totp_mod.new_recovery_codes()
            c.execute('UPDATE users SET totp_enabled=1,totp_last_step=?,totp_recovery_codes=? WHERE id=?',
                      (step, json.dumps([totp_mod.hash_recovery_code(x) for x in codes]), user['id']))
            c.commit(); c.close()
            acsec.bump_auth_version_keep_current(user['id'])
            log_security_event('2FA_ENABLED', 'SUCCESS', target='account')
            acsec.notify_user(user, 'Double authentification activée',
                              "La double authentification est désormais active sur votre compte ProfitOS.")
            return render_template('two_factor_codes.html', codes=codes)
        c.close()
        return render_template('two_factor_setup.html', secret_grouped=totp_mod.grouped(secret),
                               otpauth_uri=totp_mod.provisioning_uri(secret, user['email']))

    @app.route('/compte/2fa/desactiver', methods=['POST'])
    @login_required
    @rate_limit(5, 900)
    def two_factor_disable():
        user = current_user()
        if not acsec.verify_reauth(user) or not acsec.verify_second_factor(user, request.form.get('code', '')):
            flash('Mot de passe ou code incorrect.')
            return redirect(url_for('account_security'))
        c = auth_cx()
        c.execute('UPDATE users SET totp_enabled=0,totp_secret=NULL,totp_recovery_codes=NULL,totp_last_step=NULL WHERE id=?',
                  (user['id'],))
        c.commit(); c.close()
        log_security_event('2FA_DISABLED', 'SUCCESS', target='account')
        acsec.notify_user(user, 'Double authentification désactivée',
                          "La double authentification a été désactivée sur votre compte ProfitOS. Si ce n'était pas vous, changez votre mot de passe.",
                          cta_label='Sécuriser mon compte', cta_endpoint='forgot_password')
        flash('Double authentification désactivée.')
        return redirect(url_for('account_security'))

    @app.route('/compte/2fa/codes', methods=['POST'])
    @login_required
    @rate_limit(5, 900)
    def two_factor_recovery_codes():
        user = current_user()
        if not acsec.user_has_2fa(user) or not acsec.verify_reauth(user):
            flash('Mot de passe incorrect.')
            return redirect(url_for('account_security'))
        codes = totp_mod.new_recovery_codes()
        c = auth_cx()
        c.execute('UPDATE users SET totp_recovery_codes=? WHERE id=?',
                  (json.dumps([totp_mod.hash_recovery_code(x) for x in codes]), user['id']))
        c.commit(); c.close()
        log_security_event('2FA_RECOVERY_REGENERATED', 'SUCCESS', target='account')
        return render_template('two_factor_codes.html', codes=codes)

    @app.route('/organisation/2fa-obligatoire', methods=['POST'])
    @login_required
    def org_require_two_factor():
        if current_role() != 'OWNER':
            abort(403)
        user = current_user()
        enable = request.form.get('require') == '1'
        if enable and not acsec.user_has_2fa(user):
            flash("Activez d'abord la double authentification sur votre propre compte.")
            return redirect(url_for('account_security'))
        c = auth_cx()
        c.execute('UPDATE organizations SET require_2fa=?,updated_at=? WHERE id=?', (1 if enable else 0, now(), session['org_id']))
        c.commit(); c.close()
        log_security_event('ORG_2FA_REQUIRED', 'ENABLED' if enable else 'DISABLED', target='organization')
        flash('Double authentification désormais obligatoire pour tous les membres.' if enable
              else "La double authentification n'est plus obligatoire.")
        return redirect(request.referrer if safe_next_url(urlsplit(request.referrer or '').path) else url_for('team'))

    # ------------------------------------------------------------------ invitations d'équipe
    @app.route('/invitation/<token>', methods=['GET', 'POST'])
    @limiter.limit('30 per 900 seconds', override_defaults=True)
    def team_invitation(token):
        inv, org = _invitation_by_token(token)
        if not inv:
            flash("Cette invitation est invalide, expirée ou a déjà été utilisée.")
            return redirect(url_for('login'))
        c = auth_cx()
        invited_user = c.execute('SELECT * FROM users WHERE email=? AND is_active=1', (inv['email'],)).fetchone()
        c.close()
        me = current_user() if session.get('user_id') else None
        if request.method == 'POST':
            action = request.form.get('action')
            if action in ('accept', 'create') and not team_seat_available(inv['organization_id']):
                flash("L'équipe de cette organisation a atteint la limite de sa formule actuelle : demandez au propriétaire de libérer une place ou de changer de formule.")
                return redirect(url_for('home') if me else url_for('login'))
            if action == 'accept':
                if not me or (me['email'] or '').lower() != inv['email'].lower():
                    abort(403)
                if not me['email_verified']:
                    flash("Confirmez d'abord votre adresse e-mail (lien reçu à l'inscription) pour accepter l'invitation.")
                    return redirect(url_for('team_invitation', token=token))
                c = auth_cx()
                ok = _accept_invitation(c, inv, me['id'])
                c.commit(); c.close()
                if not ok:
                    flash("Cette invitation a déjà été utilisée."); return redirect(url_for('home'))
                session['org_id'] = inv['organization_id']; session['role'] = inv['role']; session.pop('current_entity_id', None)
                if hasattr(g, 'current_membership'): delattr(g, 'current_membership')
                init_tenant_db()
                log_activity('TEAM_INVITE_ACCEPTED', f"Invitation acceptée par {me['email']}")
                log_security_event('TEAM_INVITE_ACCEPTED', 'SUCCESS', user_id=me['id'], organization_id=inv['organization_id'], target=f"invite:{inv['id']}")
                flash(f"Vous avez rejoint « {org['name']} ».")
                return redirect(url_for('home'))
            if action == 'create':
                if invited_user or me:
                    abort(403)
                name = request.form.get('full_name', '').strip()[:120]
                pw = request.form.get('password', ''); pw2 = request.form.get('password_confirm', '')
                err = password_error(pw, inv['email'])
                if not name:
                    err = 'Votre nom est requis.'
                if not err and pw != pw2:
                    err = 'Les deux mots de passe ne correspondent pas.'
                if err:
                    flash(err); return redirect(url_for('team_invitation', token=token))
                c = auth_cx()
                # Le lien a été reçu dans la boîte de l'adresse invitée : adresse vérifiée.
                c.execute('INSERT INTO users(email,password_hash,full_name,is_active,email_verified,created_at,updated_at) VALUES(?,?,?,1,1,?,?)',
                          (inv['email'], generate_password_hash(pw), name, now(), now()))
                uid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
                ok = _accept_invitation(c, inv, uid)
                if not ok:
                    c.rollback(); c.close()
                    flash("Cette invitation a déjà été utilisée."); return redirect(url_for('login'))
                c.commit()
                user = c.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
                c.close()
                acsec.start_user_session(user, inv['organization_id'], inv['role'])
                init_tenant_db()
                log_activity('TEAM_INVITE_ACCEPTED', f"Compte créé via invitation : {inv['email']}")
                log_security_event('TEAM_INVITE_ACCEPTED', 'SUCCESS', user_id=uid, organization_id=inv['organization_id'], target=f"invite:{inv['id']}")
                flash(f"Bienvenue ! Vous avez rejoint « {org['name']} ».")
                return redirect(url_for('home'))
            abort(400)
        return render_template('team_invitation.html', inv=inv, org=org, me=me, invited_user=invited_user, token=token,
                               role_label=ROLE_LABELS.get(inv['role'], inv['role']))

    # ------------------------------------------------------------------ export personnel RGPD
    @app.route('/compte/mes-donnees')
    @login_required
    @limiter.limit('10 per 3600 seconds', override_defaults=True)
    def personal_data_export():
        from profitos.gdpr_export import build_personal_export
        data = build_personal_export(session['user_id'])
        log_security_event('PERSONAL_DATA_EXPORT', 'SUCCESS', target='account')
        return send_file(io.BytesIO(data), mimetype='application/zip', as_attachment=True,
                         download_name=f"profitos-mes-donnees-{paris_today().isoformat()}.zip", max_age=0)

    # ------------------------------------------------------------------ organisation en cours de suppression
    @app.route('/organisation/suppression-programmee')
    @login_required
    def org_deletion_pending():
        org = current_org()
        if not org or not org.get('deleted_at'):
            return redirect(url_for('home'))
        return render_template('org_deletion_pending.html', org=org, is_owner=current_role() == 'OWNER')

    @app.route('/organisation/suppression-programmee/annuler', methods=['POST'])
    @login_required
    def org_deletion_cancel():
        from profitos.retention import cancel_organization_deletion
        org = current_org()
        if not org or not org.get('deleted_at'):
            return redirect(url_for('home'))
        if current_role() != 'OWNER' or not acsec.verify_reauth(current_user()):
            flash("Seul un propriétaire peut annuler la suppression (mot de passe requis).")
            return redirect(url_for('org_deletion_pending'))
        cancel_organization_deletion(org['id'])
        log_security_event('ORG_DELETION_CANCELLED', 'SUCCESS', target='organization')
        log_activity('ORG_DELETION_CANCELLED', "Suppression de l'organisation annulée")
        flash("La suppression a été annulée : votre organisation est de nouveau accessible. Les clés API révoquées doivent être recréées.")
        return redirect(url_for('home'))

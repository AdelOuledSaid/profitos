"""Expert Network (v485) : annuaire privé, demandes de mission depuis Mission Control,
devis, suivi, livrables ; lien sécurisé pour l'expert ; administration de l'annuaire."""
from profitos.runtime import *
from profitos.feature_access import requires_paid_plan
from profitos import expert_network as EN
from profitos import verification as V
from profitos.accounting import paris_today


def _eid():
    from profitos.entities import current_entity_id
    return current_entity_id()


def _find_finding(c, key):
    """Constat courant (Mission Control, contrats, dossier, radar) pour une clé, ou None."""
    if not key:
        return None
    from profitos.routes.mission_control import _findings
    f = next((x for x in _findings(c) if x['key'] == key), None)
    if f:
        return f
    try:
        from profitos import opportunity_radar as OR
        found, _e = OR.detect(c, _eid(), paris_today())
        return next((x for x in found if x['key'] == key), None)
    except Exception:
        return None


def _mail(to, subject, intro, cta_label, cta_url, org=None):
    html = render_template('email_transactional.html', title=subject, intro=intro, cta_label=cta_label, cta_url=cta_url, footer='')
    try:
        if org is not None:
            return send_email(to, subject, html, on_behalf_of_org=org)
        return send_email(to, subject, html, kind='system')
    except Exception:
        current_app.logger.exception('Expert Network : e-mail non envoyé')
        return {'sent': False}


def _is_platform_admin():
    allowed = {x.strip().lower() for x in os.environ.get('PLATFORM_ADMIN_EMAILS', '').split(',') if x.strip()}
    if not allowed or not session.get('user_id'):
        return False
    ac = auth_cx()
    try:
        # Adresse vérifiée exigée, comme pour les autres écrans d'administration.
        u = ac.execute('SELECT email FROM users WHERE id=? AND email_verified=1', (session.get('user_id'),)).fetchone()
    finally:
        ac.close()
    return bool(u) and (u['email'] or '').strip().lower() in allowed


def register(app):

    # ------------------------------------------------------------------ entreprise
    @app.route('/experts')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('pilotage')
    def experts_directory():
        ac = auth_cx()
        try:
            rows = EN.experts(ac)
        finally:
            ac.close()
        c = cx()
        try:
            ms = EN.missions(c, _eid())
        finally:
            c.close()
        by_domain = [(code, d, [e for e in rows if e['domain'] == code]) for code, d in EN.DOMAINS.items()]
        return render_template('experts.html', by_domain=by_domain, missions=ms, domains=EN.DOMAINS)

    @app.route('/experts/demande', methods=['GET', 'POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('pilotage')
    def expert_request():
        source = (request.values.get('source') or '')[:200]
        domain = request.values.get('domaine') if request.values.get('domaine') in EN.DOMAINS else None
        c = cx()
        ac = auth_cx()
        try:
            finding = _find_finding(c, source) if source else None
            if finding and not domain:
                domain = EN.KIND_DOMAINS.get(finding['kind'])
            choices = EN.experts(ac, domain) if domain else EN.experts(ac)
            if request.method == 'POST':
                if current_role() == 'LECTURE':
                    flash("Votre rôle ne permet pas de demander une mission.")
                    return redirect(url_for('experts_directory'))
                try:
                    expert_id = int(request.form.get('expert_id') or 0)
                except ValueError:
                    expert_id = 0
                e = EN.expert(ac, expert_id)
                try:
                    mission_id, token = EN.create_mission(
                        c, ac, session['org_id'], _eid(), e, request.form.get('title'), request.form.get('request'),
                        request.form.getlist('share'), request.form.get('consent') == '1', current_user()['email'],
                        finding=finding, owner_email=current_user()['email'])
                except EN.ExpertError as exc:
                    flash(str(exc))
                    return redirect(url_for('expert_request', source=source or None, domaine=domain))
                org = current_org()
                link = f"{app_base_url().rstrip('/')}{url_for('expert_public', token=token)}"
                res = _mail(e['email'], f"Demande de mission — {org['name']}",
                            f"{org['name']} vous propose une mission ({EN.DOMAINS[e['domain']]['label']}) via ProfitOS : "
                            f"« {request.form.get('title', '')[:120]} ». Consultez le dossier et proposez un devis depuis ce lien "
                            f"personnel, valable {EN.LINK_DAYS} jours.", 'Voir la demande', link, org=org)
                log_activity('EXPERT_MISSION_SENT', f"Mission envoyée à {e['name']} : {request.form.get('title', '')[:100]}")
                flash(f"Demande envoyée à {e['name']}." if res.get('sent') and not res.get('dry_run') else
                      f"Demande enregistrée ; l'e-mail à {e['name']} n'a pas pu partir (service e-mail non configuré) : "
                      "transmettez-lui le lien affiché sur la mission.")
                session[f'expert_link_{mission_id}'] = link
                return redirect(url_for('expert_mission', mission_id=mission_id))
        finally:
            ac.close()
            c.close()
        return render_template('expert_request.html', finding=finding, source=source, domain=domain, choices=choices,
                               domains=EN.DOMAINS, share_items=EN.SHARE_ITEMS,
                               preselect=request.args.get('expert', type=int))

    @app.route('/experts/missions/<int:mission_id>')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('pilotage')
    def expert_mission(mission_id):
        c = cx()
        try:
            m = EN.mission(c, _eid(), mission_id)
            if not m:
                abort(404)
            result = EN.source_result(c, _eid(), m)
        finally:
            c.close()
        return render_template('expert_mission.html', m=m, result=result, share_items=EN.SHARE_ITEMS,
                               fresh_link=session.pop(f'expert_link_{mission_id}', None))

    @app.post('/experts/missions/<int:mission_id>/action')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('pilotage')
    def expert_mission_action(mission_id):
        op = request.form.get('op')
        user = current_user()['email']
        org = current_org()
        c = cx()
        try:
            m = EN.mission(c, _eid(), mission_id)
            if not m:
                abort(404)
            try:
                if op == 'accept':
                    EN.client_accept(c, m, user)
                    if m.get('source_key'):
                        # Le constat d'origine reste « en contrôle » : il ne sera soldé que par les données.
                        V.ensure_table(c)
                        if c.execute("SELECT 1 FROM verification_items WHERE entity_id IS ? AND item_key=? AND status IN ('ouvert','en_controle')",
                                     (_eid(), m['source_key'])).fetchone():
                            V.record_action(c, _eid(), m['source_key'], 'Expert mandaté',
                                            f"{m['expert_name']} mandaté (devis de {fr_number(m['quote_amount'], 2)} € HT accepté)",
                                            user, paris_today(), int(m['quote_delay_days'] or 14) + 7)
                    # Le lien de l'expert n'est pas conservé (seule son empreinte l'est) : on le renvoie à son e-mail initial.
                    _mail(m['expert_email'], f"Devis accepté — {org['name']}",
                          f"{org['name']} a accepté votre devis pour « {m['title']} ». Vous pouvez démarrer la mission ; "
                          "le suivi, les messages et la remise du livrable se font depuis le lien reçu avec la demande.",
                          None, None, org=org)
                    flash('Devis accepté. Le résultat sera contrôlé dans vos données à la fin de la mission.')
                elif op == 'refuse':
                    EN.client_refuse(c, m, user, request.form.get('reason'))
                    ac = auth_cx()
                    try:
                        EN.revoke_links(ac, session['org_id'], mission_id)
                    finally:
                        ac.close()
                    flash('Devis refusé.')
                elif op == 'message':
                    EN.message(c, m, 'client', request.form.get('text'), request.form.get('url'))
                    flash('Message ajouté au suivi de la mission.')
                elif op == 'complete':
                    EN.client_complete(c, m, user, request.form.get('note'))
                    rating = request.form.get('rating')
                    ac = auth_cx()
                    try:
                        EN.ensure_directory(ac)
                        if rating and rating.isdigit() and 1 <= int(rating) <= 5:
                            ac.execute('INSERT INTO expert_feedback(expert_id,organization_id,mission_id,rating,comment,created_at) VALUES(?,?,?,?,?,?)',
                                       (m['expert_id'], session['org_id'], mission_id, int(rating), (request.form.get('note') or '')[:500], now()))
                            ac.commit()
                        EN.revoke_links(ac, session['org_id'], mission_id)
                    finally:
                        ac.close()
                    flash('Mission terminée.')
                elif op == 'cancel':
                    EN.client_cancel(c, m, user)
                    ac = auth_cx()
                    try:
                        EN.revoke_links(ac, session['org_id'], mission_id)
                    finally:
                        ac.close()
                    flash("Mission annulée ; le lien de l'expert est désactivé.")
                else:
                    abort(400)
            except EN.ExpertError as exc:
                flash(str(exc))
            log_activity('EXPERT_MISSION_ACTION', f"Mission {mission_id} : {op}")
        finally:
            c.close()
        return redirect(url_for('expert_mission', mission_id=mission_id))

    # ------------------------------------------------------------------ expert (lien sécurisé)
    def _resolve(token):
        ac = auth_cx()
        try:
            found = EN.resolve_link(ac, token)
            e = EN.expert(ac, found[2]) if found else None
        finally:
            ac.close()
        if not found or not e:
            abort(404)
        org_id, mission_id, _expert_id = found
        tc = tenant_cx_direct(org_id)
        m = EN.mission_any_entity(tc, mission_id)
        if not m or m['expert_id'] != e['id']:
            tc.close()
            abort(404)
        return tc, m, e, org_id

    @app.route('/expert/mission/<token>', methods=['GET', 'POST'])
    def expert_public(token):
        tc, m, e, org_id = _resolve(token)
        try:
            if request.method == 'POST':
                op = request.form.get('op')
                try:
                    if op == 'quote':
                        EN.expert_quote(tc, m, parse_amount(request.form.get('amount') or ''), request.form.get('delay_days') or 0,
                                        request.form.get('text'))
                        note = f"{e['name']} a proposé un devis de {request.form.get('amount')} € HT pour « {m['title']} »."
                    elif op == 'decline':
                        EN.expert_decline(tc, m, request.form.get('reason'))
                        note = f"{e['name']} a décliné la mission « {m['title']} »."
                    elif op == 'deliver':
                        EN.expert_deliver(tc, m, request.form.get('text'), request.form.get('url'))
                        note = f"{e['name']} a remis un livrable pour « {m['title']} » : à valider."
                    elif op == 'message':
                        EN.message(tc, m, 'expert', request.form.get('text'), request.form.get('url'))
                        note = f"Nouveau message de {e['name']} sur « {m['title']} »."
                    else:
                        abort(400)
                    flash('Enregistré. Votre interlocuteur est prévenu.')
                    if m.get('created_by'):
                        _mail(m['created_by'], f"Mission « {m['title'][:60]} » — {e['name']}", note, 'Ouvrir la mission',
                              f"{app_base_url().rstrip('/')}{url_for('expert_mission', mission_id=m['id'])}")
                except EN.ExpertError as exc:
                    flash(str(exc))
                return redirect(url_for('expert_public', token=token))
            ac = auth_cx()
            try:
                org = ac.execute('SELECT name FROM organizations WHERE id=?', (org_id,)).fetchone()
            finally:
                ac.close()
            m = EN.mission_any_entity(tc, m['id'])
        finally:
            tc.close()
        return render_template('expert_public.html', m=m, e=e, org_name=org['name'] if org else '', token=token,
                               share_items=EN.SHARE_ITEMS, title='Mission ProfitOS')

    # ------------------------------------------------------------------ administration de l'annuaire
    @app.route('/admin/experts', methods=['GET', 'POST'])
    @login_required
    def experts_admin():
        if not _is_platform_admin():
            abort(403)
        ac = auth_cx()
        try:
            if request.method == 'POST':
                expert_id = request.form.get('expert_id', type=int)
                try:
                    EN.save_expert(ac, request.form, current_user()['email'], expert_id,
                                   verified=request.form.get('verified') == '1')
                    flash('Expert enregistré.')
                except EN.ExpertError as exc:
                    flash(str(exc))
                return redirect(url_for('experts_admin'))
            rows = EN.experts(ac, proposable_only=False)
            edit = EN.expert(ac, request.args.get('modifier', type=int)) if request.args.get('modifier') else None
        finally:
            ac.close()
        return render_template('experts_admin.html', rows=rows, edit=edit, domains=EN.DOMAINS)

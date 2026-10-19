"""Écrans des télédéclarations : préparation contrôlée, dépôt, suivi des accusés."""
import json
from datetime import date, timedelta

from profitos.runtime import *
from profitos.accounting import paris_today
from profitos import ledger as L
from profitos import teledeclaration as T

SUBMIT_ROLES = ('OWNER', 'ADMIN', 'COMPTABLE')
RECEIPT_CATEGORY = 'declaration_receipts'


def _simulation_allowed():
    """Sans partenaire EDI, le dépôt simulé ne sert qu'aux tests et au développement :
    en production, l'entreprise prépare sa déclaration et la dépose sur impots.gouv.fr."""
    import os
    return os.environ.get('PROFITOS_ENV', 'production') == 'development' or os.environ.get('EDI_SIMULATION_ENABLED') == '1'


def _eid():
    from profitos.entities import current_entity_id
    return current_entity_id()


def _identity(c, eid):
    from profitos.entities import resolve_entity
    try:
        return dict(resolve_entity(c, eid))
    except Exception:
        return {}


def _previous_month():
    first = paris_today().replace(day=1)
    end = first - timedelta(days=1)
    return end.replace(day=1), end


def _closed_fy(c, eid):
    current = L.fiscal_year_for(c, paris_today(), eid)
    return L.fiscal_year_for(c, current['start'] - timedelta(days=1), eid), current


def _period_from_args(c, eid, decl_type, args):
    du, au = (args.get('du') or '').strip(), (args.get('au') or '').strip()
    if du and au:
        return date.fromisoformat(du), date.fromisoformat(au)
    if decl_type == 'TVA_CA3':
        return _previous_month()
    if decl_type == 'DAS2':
        y = paris_today().year - 1
        return date(y, 1, 1), date(y, 12, 31)
    closed, current = _closed_fy(c, eid)
    if decl_type == 'IS_2571':
        return closed['start'], closed['end']
    return closed['start'], closed['end']


def _build(c, eid, decl_type, start, end, args):
    from profitos import corporate_tax as CT
    regime = args.get('regime') if args.get('regime') in ('normal', 'simplifie') else \
        (CT.get_fiscal_profile(c, eid).get('tax_regime') or 'normal')
    instalment = None
    if decl_type in ('IS_2571', 'TVA_3514') and (args.get('acompte') or '').strip():
        instalment = form_float(args.get('acompte'))
    due = (args.get('echeance') or '').strip() or None
    if due:
        date.fromisoformat(due)
    return T.build_declaration(c, decl_type, start, end, eid, identity=_identity(c, eid), regime=regime,
                               instalment_amount=instalment, due_date=due), regime


def register(app):
    app.jinja_env.globals['teledeclaration_status_label'] = T.status_label
    app.jinja_env.globals['declarations_open'] = lambda: T.declarations_open(session.get('org_id'))

    CLOSED_MSG = ("La préparation des déclarations est en cours de validation et n'est pas encore ouverte : "
                  "les montants restent consultables à titre indicatif.")

    def _closed():
        return not T.declarations_open(session.get('org_id'))
    from profitos.corporate_tax import masked_iban
    app.jinja_env.globals['masked_iban'] = masked_iban

    @app.route('/comptabilite/teledeclarations')
    @login_required
    def teledeclarations():
        from profitos import corporate_tax as CT
        eid = _eid()
        c = cx()
        rows = T.list_declarations(c, eid)
        closed, current = _closed_fy(c, eid)
        m_start, m_end = _previous_month()
        q_end = m_end
        q_start = date(q_end.year, ((q_end.month - 1) // 3) * 3 + 1, 1)
        instalments = [d for d in CT.instalment_dates(current['start'], current['end'])]
        profile = CT.get_fiscal_profile(c, eid)
        profile_vat = profile.get('vat_filing') or 'ca3_mensuelle'
        vat_instalments = []
        if profile_vat == 'ca12':
            from profitos.vat_return import ca12_form
            vat_instalments = ca12_form(c, closed['start'].isoformat(), closed['end'].isoformat(), eid)['instalments']
        c.close()
        active = {(r['decl_type'], r['period_start'], r['period_end']): r for r in rows
                  if r['status'] in T.ACTIVE_STATUSES and r['mode'] != 'simulation'}
        inst_active = {(r['decl_type'], r['due_date']): r for r in reversed(rows)
                       if (r['status'] in T.ACTIVE_STATUSES or r['status'] == 'prepared') and r['mode'] != 'simulation'
                       and r['decl_type'] in T.INSTALMENT_TYPES
                       and r['period_end'] == closed['end'].isoformat()}
        vat_filing = profile_vat
        vat_candidates = {
            'ca3_mensuelle': [{'type': 'TVA_CA3', 'title': 'TVA du mois', 'start': m_start, 'end': m_end}],
            'ca3_trimestrielle': [{'type': 'TVA_CA3', 'title': 'TVA du trimestre', 'start': q_start, 'end': q_end}],
            'ca12': [{'type': 'TVA_CA12', 'title': "TVA annuelle de l'exercice clos", 'start': closed['start'], 'end': closed['end']}],
            'franchise': [],
        }.get(vat_filing, [])
        candidates = vat_candidates + [
            {'type': 'LIASSE', 'title': f"Liasse de l'exercice clos le {closed['end'].strftime('%d/%m/%Y')}",
             'start': closed['start'], 'end': closed['end']},
            {'type': 'IS_2572', 'title': "Solde d'IS de l'exercice clos", 'start': closed['start'], 'end': closed['end']},
            {'type': 'CVAE_1330', 'title': "Valeur ajoutée de l'exercice clos", 'start': closed['start'], 'end': closed['end']},
            {'type': 'DAS2', 'title': f"Honoraires versés en {paris_today().year - 1}",
             'start': date(paris_today().year - 1, 1, 1), 'end': date(paris_today().year - 1, 12, 31)},
        ]
        prepared = {(r['decl_type'], r['period_start'], r['period_end']): r for r in reversed(rows) if r['status'] == 'prepared'}
        for cand in candidates:
            cand['meta'] = T.TYPES[cand['type']]
            key = (cand['type'], cand['start'].isoformat(), cand['end'].isoformat())
            cand['existing'] = active.get(key) or prepared.get(key)
            cand['channel'] = T.filing_channel(cand['type'], profile.get('tax_regime') or 'normal')
        return render_template('teledeclarations.html', rows=rows, candidates=candidates, instalments=instalments,
                               vat_instalments=vat_instalments, inst_active=inst_active, vat_filing=profile_vat, mandate_ok=CT.mandate_ready(profile),
                               closed=closed, partner=T.partner_configured(), types=T.TYPES,
                               can_submit=current_role() in SUBMIT_ROLES)

    @app.route('/comptabilite/teledeclarations/preparer')
    @login_required
    def teledeclaration_prepare():
        decl_type = request.args.get('type') or ''
        if decl_type not in T.TYPES:
            abort(404)
        eid = _eid()
        c = cx()
        try:
            start, end = _period_from_args(c, eid, decl_type, request.args)
            built, regime = _build(c, eid, decl_type, start, end, request.args)
        except ValueError as exc:
            c.close()
            flash(f"Préparation impossible : {exc}")
            return redirect(url_for('teledeclarations'))
        existing = T.active_declaration(c, eid, decl_type, start, end, built['payload'].get('due_date'))
        prepared = T.prepared_declaration(c, eid, decl_type, start, end, built['payload'].get('due_date'))
        c.close()
        return render_template('teledeclaration_prepare.html', built=built, decl_type=decl_type, start=start, end=end,
                               regime=regime, existing=existing, prepared=prepared, partner=T.partner_configured(),
                               channel=T.filing_channel(decl_type, regime), guide=T.efi_guide(decl_type, regime),
                               simulation=_simulation_allowed(),
                               can_submit=current_role() in SUBMIT_ROLES, acompte=request.args.get('acompte') or '')

    @app.post('/comptabilite/teledeclarations/deposer')
    @login_required
    def teledeclaration_submit():
        decl_type = request.form.get('type') or ''
        if decl_type not in T.TYPES:
            abort(404)
        back = url_for('teledeclaration_prepare', type=decl_type, du=request.form.get('du'), au=request.form.get('au'),
                       regime=request.form.get('regime'), acompte=request.form.get('acompte') or None,
                       echeance=request.form.get('echeance') or None)
        if _closed():
            flash(CLOSED_MSG)
            return redirect(back)
        if current_role() not in SUBMIT_ROLES:
            flash("Seuls le propriétaire, un administrateur ou le comptable peuvent déposer une déclaration.")
            return redirect(back)
        if not request.form.get('certify'):
            flash("Cochez la certification avant de déposer la déclaration.")
            return redirect(back)
        if not T.partner_configured() and not _simulation_allowed():
            flash("Aucun partenaire EDI n'est connecté : préparez la déclaration puis déposez-la sur impots.gouv.fr.")
            return redirect(back)
        eid = _eid()
        c = cx()
        try:
            start, end = _period_from_args(c, eid, decl_type, request.form)
            built, _regime = _build(c, eid, decl_type, start, end, request.form)
            decl_id = T.submit_declaration(c, built, eid, current_user()['email'],
                                           corrective=bool(request.form.get('corrective')))
        except T.TeledeclarationError as exc:
            c.close()
            flash(str(exc))
            return redirect(back)
        except ValueError as exc:
            c.close()
            flash(f"Dépôt impossible : {exc}")
            return redirect(back)
        c.close()
        mode = 'production' if T.partner_configured() else 'simulation'
        due_txt = f", échéance {built['payload']['due_date']}" if built['payload'].get('due_date') else ''
        log_activity('TELEDECLARATION_SUBMITTED', f"{T.TYPES[decl_type]['form']} du {start.isoformat()} au {end.isoformat()}{due_txt} ({mode})")
        log_security_event('TELEDECLARATION_SUBMITTED', 'SUCCESS', target=f"{decl_type}:{end.isoformat()}:{mode}")
        flash("Déclaration déposée chez le partenaire EDI." if mode == 'production'
              else "Déclaration déposée en mode simulation : rien n'a été transmis à l'administration.")
        return redirect(url_for('teledeclaration_detail', decl_id=decl_id))

    def _row_or_404(c, decl_id, eid):
        ef = 'entity_id=?' if eid else 'entity_id IS NULL'
        row = c.execute(f'SELECT * FROM teledeclarations WHERE id=? AND {ef}', (decl_id, *((eid,) if eid else ()))).fetchone()
        if not row:
            c.close()
            abort(404)
        return row

    @app.route('/comptabilite/teledeclarations/<int:decl_id>')
    @login_required
    def teledeclaration_detail(decl_id):
        eid = _eid()
        c = cx()
        row = _row_or_404(c, decl_id, eid)
        c.close()
        payload = json.loads(row['payload_json'] or '{}')
        return render_template('teledeclaration_detail.html', row=row, payload=payload,
                               history=json.loads(row['history_json'] or '[]'),
                               reasons=json.loads(row['rejection_json'] or '[]'), types=T.TYPES,
                               guide=T.efi_guide(row['decl_type'], payload.get('regime') or 'normal'),
                               impots_url=T.IMPOTS_GOUV_URL, can_submit=current_role() in SUBMIT_ROLES,
                               receipt_check=json.loads(row['receipt_check']) if row['receipt_check'] else None,
                               today=paris_today().isoformat())

    @app.post('/comptabilite/teledeclarations/preparer-depot')
    @login_required
    def teledeclaration_prepare_manual():
        decl_type = request.form.get('type') or ''
        if decl_type not in T.TYPES:
            abort(404)
        back = url_for('teledeclaration_prepare', type=decl_type, du=request.form.get('du'), au=request.form.get('au'),
                       regime=request.form.get('regime'), acompte=request.form.get('acompte') or None,
                       echeance=request.form.get('echeance') or None)
        if _closed():
            flash(CLOSED_MSG)
            return redirect(back)
        if current_role() not in SUBMIT_ROLES:
            flash("Seuls le propriétaire, un administrateur ou le comptable peuvent préparer une déclaration.")
            return redirect(back)
        eid = _eid()
        c = cx()
        try:
            start, end = _period_from_args(c, eid, decl_type, request.form)
            built, regime = _build(c, eid, decl_type, start, end, request.form)
            decl_id = T.prepare_manual(c, built, eid, current_user()['email'], regime=regime,
                                       corrective=bool(request.form.get('corrective')))
        except (T.TeledeclarationError, ValueError) as exc:
            c.close()
            flash(str(exc) if isinstance(exc, T.TeledeclarationError) else f"Préparation impossible : {exc}")
            return redirect(back)
        c.close()
        log_activity('DECLARATION_PREPARED', f"{T.TYPES[decl_type]['form']} du {start.isoformat()} au {end.isoformat()} préparée")
        flash("Déclaration préparée : téléchargez le récapitulatif, déposez-la sur impots.gouv.fr puis archivez ici le justificatif.")
        return redirect(url_for('teledeclaration_detail', decl_id=decl_id))

    @app.route('/comptabilite/teledeclarations/<int:decl_id>/recapitulatif.pdf')
    @login_required
    def teledeclaration_pdf(decl_id):
        from profitos.declaration_pdf import render_declaration_pdf
        eid = _eid()
        c = cx()
        row = _row_or_404(c, decl_id, eid)
        c.close()
        payload = json.loads(row['payload_json'] or '{}')
        meta = T.TYPES.get(row['decl_type'], {'label': row['form']})
        data = render_declaration_pdf(row, payload, meta, T.efi_guide(row['decl_type'], payload.get('regime') or 'normal'),
                                      T.IMPOTS_GOUV_URL)
        if data is None:
            flash("Génération du PDF indisponible sur ce serveur.")
            return redirect(url_for('teledeclaration_detail', decl_id=decl_id))
        return Response(data, mimetype='application/pdf', headers={
            'Content-Disposition': f'attachment; filename="recapitulatif-{row["form"].split()[0].lower()}-{row["period_end"]}.pdf"'})

    @app.post('/comptabilite/teledeclarations/<int:decl_id>/justificatif')
    @login_required
    def teledeclaration_receipt_upload(decl_id):
        import uuid
        from profitos import storage as _st
        eid = _eid()
        if _closed():
            flash(CLOSED_MSG)
            return redirect(url_for('teledeclaration_detail', decl_id=decl_id))
        if current_role() not in SUBMIT_ROLES:
            flash("Seuls le propriétaire, un administrateur ou le comptable peuvent enregistrer un dépôt.")
            return redirect(url_for('teledeclaration_detail', decl_id=decl_id))
        c = cx()
        row = _row_or_404(c, decl_id, eid)
        upload = request.files.get('receipt')
        stored = None
        try:
            if not upload or not upload.filename:
                raise T.TeledeclarationError("Joignez le justificatif de dépôt (accusé de réception d'impots.gouv.fr).")
            data = upload.read(T.MAX_RECEIPT_BYTES + 1)
            ext = T.check_receipt(upload.filename, data)
            filed_on = (request.form.get('filed_on') or '').strip()
            if not filed_on:
                raise T.TeledeclarationError("Indiquez la date du dépôt.")
            check = T.analyze_receipt(data, ext, row, json.loads(row['payload_json'] or '{}'))
            stored = uuid.uuid4().hex + ext
            _st.write_bytes(RECEIPT_CATEGORY, session['org_id'], stored, data)
            T.record_filing(c, row, stored, secure_filename(upload.filename)[:200] or 'justificatif' + ext, filed_on,
                            (request.form.get('reference') or '').strip()[:80] or None, current_user()['email'], check=check)
            if row['receipt_stored'] and row['receipt_stored'] != stored:
                _st.delete(RECEIPT_CATEGORY, session['org_id'], row['receipt_stored'])
        except (T.TeledeclarationError, ValueError) as exc:
            if stored:
                _st.delete(RECEIPT_CATEGORY, session['org_id'], stored)
            c.close()
            flash(str(exc) if isinstance(exc, T.TeledeclarationError) else "Date de dépôt invalide.")
            return redirect(url_for('teledeclaration_detail', decl_id=decl_id))
        c.close()
        log_activity('DECLARATION_FILED', f"{row['form']} du {row['period_start']} au {row['period_end']} : justificatif archivé")
        log_security_event('DECLARATION_FILED', 'SUCCESS', target=f"{row['decl_type']}:{row['period_end']}")
        if check['status'] == 'concordant':
            flash("Dépôt confirmé : l'accusé joint correspond à la déclaration préparée (SIREN, formulaire, période, montant).")
        elif check['status'] == 'ecarts':
            flash("Justificatif à vérifier : l'accusé ne correspond pas entièrement à la déclaration préparée ("
                  + ', '.join(c_['label'] for c_ in check['checks'] if not c_['ok'])
                  + "). Joignez le bon accusé, ou confirmez le dépôt en expliquant l'écart.")
        else:
            flash(f"Dépôt déclaré : l'accusé est archivé mais n'a pas pu être vérifié automatiquement ({check['reason']}). "
                  "Contrôlez-le puis confirmez le dépôt.")
        return redirect(url_for('teledeclaration_detail', decl_id=decl_id))

    @app.post('/comptabilite/teledeclarations/<int:decl_id>/confirmer')
    @login_required
    def teledeclaration_confirm(decl_id):
        eid = _eid()
        if _closed():
            flash(CLOSED_MSG)
            return redirect(url_for('teledeclaration_detail', decl_id=decl_id))
        if current_role() not in SUBMIT_ROLES:
            flash("Seuls le propriétaire, un administrateur ou le comptable peuvent confirmer un dépôt.")
            return redirect(url_for('teledeclaration_detail', decl_id=decl_id))
        c = cx()
        row = _row_or_404(c, decl_id, eid)
        try:
            if not request.form.get('verified'):
                raise T.TeledeclarationError("Cochez « j'ai vérifié l'accusé » pour confirmer le dépôt.")
            T.confirm_filing(c, row, current_user()['email'], request.form.get('note') or '')
        except T.TeledeclarationError as exc:
            c.close()
            flash(str(exc))
            return redirect(url_for('teledeclaration_detail', decl_id=decl_id))
        c.close()
        log_activity('DECLARATION_CONFIRMED', f"{row['form']} du {row['period_start']} au {row['period_end']} : dépôt confirmé manuellement")
        log_security_event('DECLARATION_CONFIRMED_MANUALLY', 'SUCCESS', target=f"{row['decl_type']}:{row['period_end']}")
        flash("Dépôt confirmé après vérification manuelle de l'accusé.")
        return redirect(url_for('teledeclaration_detail', decl_id=decl_id))

    @app.route('/comptabilite/teledeclarations/<int:decl_id>/justificatif')
    @login_required
    def teledeclaration_receipt(decl_id):
        from profitos import storage as _st
        eid = _eid()
        c = cx()
        row = _row_or_404(c, decl_id, eid)
        c.close()
        if not row['receipt_stored']:
            abort(404)
        try:
            data = _st.read_bytes(RECEIPT_CATEGORY, session['org_id'], row['receipt_stored'])
        except (OSError, ValueError):
            abort(404)
        ext = row['receipt_stored'].rsplit('.', 1)[-1].lower()
        mime = {'pdf': 'application/pdf', 'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg'}.get(ext, 'application/octet-stream')
        return Response(data, mimetype=mime, headers={
            'Content-Disposition': f'attachment; filename="{row["receipt_name"] or "justificatif." + ext}"',
            'X-Content-Type-Options': 'nosniff'})

    @app.post('/comptabilite/teledeclarations/<int:decl_id>/actualiser')
    @login_required
    def teledeclaration_refresh(decl_id):
        eid = _eid()
        c = cx()
        row = _row_or_404(c, decl_id, eid)
        try:
            status = T.refresh_status(c, row)
            flash(f"Statut : {T.status_label(status, row['mode'])}.")
        except (T.TeledeclarationError, ValueError) as exc:
            flash(f"Statut indisponible : {exc}")
        except Exception:
            app.logger.exception('Actualisation de télédéclaration %s impossible', decl_id)
            flash("Le partenaire EDI ne répond pas : réessayez plus tard.")
        c.close()
        return redirect(url_for('teledeclaration_detail', decl_id=decl_id))

    @app.route('/comptabilite/teledeclarations/<int:decl_id>/contenu.json')
    @login_required
    def teledeclaration_payload(decl_id):
        eid = _eid()
        c = cx()
        row = _row_or_404(c, decl_id, eid)
        c.close()
        return Response(row['payload_json'], mimetype='application/json',
                        headers={'Content-Disposition': f'attachment; filename="teledeclaration-{decl_id}.json"'})

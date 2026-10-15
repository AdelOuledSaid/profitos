"""Écrans des télédéclarations : préparation contrôlée, dépôt, suivi des accusés."""
import json
from datetime import date, timedelta

from profitos.runtime import *
from profitos.accounting import paris_today
from profitos import ledger as L
from profitos import teledeclaration as T

SUBMIT_ROLES = ('OWNER', 'ADMIN', 'COMPTABLE')


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
    closed, current = _closed_fy(c, eid)
    if decl_type == 'IS_2571':
        return closed['start'], closed['end']
    return closed['start'], closed['end']


def _build(c, eid, decl_type, start, end, args):
    from profitos import corporate_tax as CT
    regime = args.get('regime') if args.get('regime') in ('normal', 'simplifie') else 'normal'
    instalment = None
    if decl_type == 'IS_2571' and (args.get('acompte') or '').strip():
        instalment = form_float(args.get('acompte'))
    return T.build_declaration(c, decl_type, start, end, eid, identity=_identity(c, eid), regime=regime,
                               instalment_amount=instalment), regime


def register(app):
    app.jinja_env.globals['teledeclaration_status_label'] = lambda s: T.STATUS_LABELS.get(s, s)

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
        c.close()
        active = {(r['decl_type'], r['period_start'], r['period_end']): r for r in rows if r['status'] in T.ACTIVE_STATUSES}
        candidates = [
            {'type': 'TVA_CA3', 'title': 'TVA du mois', 'start': m_start, 'end': m_end},
            {'type': 'TVA_CA3', 'title': 'TVA du trimestre', 'start': q_start, 'end': q_end},
            {'type': 'LIASSE', 'title': f"Liasse de l'exercice clos le {closed['end'].strftime('%d/%m/%Y')}",
             'start': closed['start'], 'end': closed['end']},
            {'type': 'IS_2572', 'title': "Solde d'IS de l'exercice clos", 'start': closed['start'], 'end': closed['end']},
            {'type': 'CVAE_1330', 'title': "Valeur ajoutée de l'exercice clos", 'start': closed['start'], 'end': closed['end']},
        ]
        for cand in candidates:
            cand['meta'] = T.TYPES[cand['type']]
            cand['existing'] = active.get((cand['type'], cand['start'].isoformat(), cand['end'].isoformat()))
        return render_template('teledeclarations.html', rows=rows, candidates=candidates, instalments=instalments,
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
        existing = T.active_declaration(c, eid, decl_type, start, end)
        c.close()
        return render_template('teledeclaration_prepare.html', built=built, decl_type=decl_type, start=start, end=end,
                               regime=regime, existing=existing, partner=T.partner_configured(),
                               can_submit=current_role() in SUBMIT_ROLES, acompte=request.args.get('acompte') or '')

    @app.post('/comptabilite/teledeclarations/deposer')
    @login_required
    def teledeclaration_submit():
        decl_type = request.form.get('type') or ''
        if decl_type not in T.TYPES:
            abort(404)
        back = url_for('teledeclaration_prepare', type=decl_type, du=request.form.get('du'), au=request.form.get('au'),
                       regime=request.form.get('regime'), acompte=request.form.get('acompte') or None)
        if current_role() not in SUBMIT_ROLES:
            flash("Seuls le propriétaire, un administrateur ou le comptable peuvent déposer une déclaration.")
            return redirect(back)
        if not request.form.get('certify'):
            flash("Cochez la certification avant de déposer la déclaration.")
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
        log_activity('TELEDECLARATION_SUBMITTED', f"{T.TYPES[decl_type]['form']} du {start.isoformat()} au {end.isoformat()} ({mode})")
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
                               reasons=json.loads(row['rejection_json'] or '[]'), types=T.TYPES)

    @app.post('/comptabilite/teledeclarations/<int:decl_id>/actualiser')
    @login_required
    def teledeclaration_refresh(decl_id):
        eid = _eid()
        c = cx()
        row = _row_or_404(c, decl_id, eid)
        try:
            status = T.refresh_status(c, row)
            flash(f"Statut : {T.STATUS_LABELS.get(status, status)}.")
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

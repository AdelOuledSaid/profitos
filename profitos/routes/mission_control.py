"""Mission Control (v480) : l'écran unique « problème → vérification → action préparée →
résultat contrôlé ». Les constats viennent de profitos/mission_control.py, le moteur de
vérification de profitos/verification.py."""
from profitos.runtime import *
from profitos.feature_access import requires_paid_plan
from profitos import mission_control as MC
from profitos import verification as V
from profitos.accounting import paris_today

# Domaine d'accès des pages vers lesquelles Mission Control renvoie (celles dont le module
# n'est pas rattaché à un domaine dans MODULE_AREAS).
LINK_AREAS = {'invoicing_detail': 'invoicing', 'purchase_detail': 'invoicing', 'purchase_sepa_batch': 'invoicing',
              'accounting_document_requests': 'accounting', 'banking': 'banking'}
SNOOZE_DAYS = 7


def _eid():
    from profitos.entities import current_entity_id
    return current_entity_id()


def _area(endpoint):
    if endpoint in LINK_AREAS:
        return LINK_AREAS[endpoint]
    fn = current_app.view_functions.get(endpoint)
    return ENDPOINT_AREAS.get(endpoint) or MODULE_AREAS.get(getattr(fn, '__module__', ''))


def _link(spec):
    """Lien {'label', 'endpoint', 'args'} ou {'label', 'url'} -> {'label', 'url'} si l'utilisateur
    a accès à la page, sinon None."""
    if spec.get('url'):
        return {'label': spec['label'], 'url': spec['url']}
    endpoint = spec.get('endpoint')
    if not endpoint or endpoint not in current_app.view_functions:
        return None
    area = _area(endpoint)
    if area and not can_access(area):
        return None
    try:
        return {'label': spec['label'], 'url': url_for(endpoint, **(spec.get('args') or {}))}
    except Exception:
        return None


def _cash():
    """Prévision de trésorerie de l'entité active, ou None si indisponible (formule, données)."""
    if not can_access('pilotage'):
        return None
    try:
        from profitos.routes.cash_intelligence import build_cash_intelligence
        return build_cash_intelligence()
    except Exception:
        current_app.logger.exception('Mission Control : prévision de trésorerie indisponible')
        return None


def _accounting_enabled():
    org = current_org()
    try:
        return bool(org) and plan_feature_enabled(effective_plan(org), 'accounting_core') and can_access('accounting')
    except Exception:
        return False


def _findings(c):
    return MC.run(c, _eid(), paris_today(), cash=_cash(), accounting=_accounting_enabled())


def _decorate(f):
    action = f.get('action') or {}
    f = dict(f)
    f['links'] = [x for x in (_link(a) for a in f.get('alternatives') or []) if x]
    f['main_link'] = _link(action) if action.get('kind') == 'link' else None
    f['can_run'] = V.can_execute(f) and can_access(action.get('area') or 'pilotage') and current_role() != 'LECTURE'
    return f


def register(app):

    @app.route('/mission-control')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('pilotage')
    def mission_control():
        c = cx()
        try:
            visible = [_decorate(f) for f in _findings(c)]
            eid = _eid()
            tracked = V.items(c, eid)
        finally:
            c.close()
        today = paris_today()
        recent = (today - timedelta(days=30)).isoformat()
        groups = {
            V.READY: [f for f in visible if f['track']['status'] == V.OPEN and f['level'] == V.READY],
            V.TO_CHECK: [f for f in visible if f['track']['status'] == V.OPEN and f['level'] == V.TO_CHECK],
            V.BLOCKED: [f for f in visible if f['track']['status'] == V.OPEN and f['level'] == V.BLOCKED],
        }
        in_control = [f for f in visible if f['track']['status'] == V.IN_CONTROL]
        resolved = sorted([t for t in tracked if t['status'] == V.RESOLVED and (t['resolved_at'] or '') >= recent],
                          key=lambda t: t['resolved_at'] or '', reverse=True)[:15]
        set_aside = [t for t in tracked if t['status'] in (V.DISMISSED, V.SNOOZED)]
        return render_template('mission_control.html', groups=groups, in_control=in_control, resolved=resolved,
                               set_aside=set_aside, level_labels=V.LEVEL_LABELS, severity_labels=V.SEVERITY_LABELS,
                               kind_labels=MC.KIND_LABELS, today=today, snooze_days=SNOOZE_DAYS)

    def _find(c, key):
        return next((f for f in _findings(c) if f['key'] == key), None)

    @app.post('/mission-control/agir')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('pilotage')
    def mission_control_execute():
        key = request.form.get('key') or ''
        c = cx()
        try:
            # Le constat est recalculé maintenant : on n'agit jamais d'après la page affichée.
            f = _find(c, key)
            if not f or f['track']['status'] != V.OPEN:
                flash("Ce point n'est plus à traiter : la situation a changé depuis l'affichage de la page.")
                return redirect(url_for('mission_control'))
            area = (f.get('action') or {}).get('area') or 'pilotage'
            if not can_access(area):
                flash("Votre rôle ne permet pas cette action.")
                return redirect(url_for('mission_control'))
            if not V.can_execute(f):
                failed = [ch['label'] for ch in f['checks'] if ch['ok'] is not True]
                flash("Action non lancée : vérifications non validées (" + ', '.join(failed) + ').')
                return redirect(url_for('mission_control'))
            ok, message = MC.execute(c, _eid(), f, current_org())
            if ok:
                V.record_action(c, _eid(), key, f['action']['label'], message, current_user()['email'], paris_today(),
                                f['follow_up_days'])
                log_activity('MISSION_CONTROL_ACTION', f"{f['title']} : {message}"[:300])
                flash(f"{message}. Résultat contrôlé automatiquement à partir du "
                      f"{(paris_today() + timedelta(days=f['follow_up_days'])).strftime('%d/%m/%Y')}.")
            else:
                flash(message)
        finally:
            c.close()
        return redirect(url_for('mission_control'))

    @app.post('/mission-control/suivi')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('pilotage')
    def mission_control_track():
        key = request.form.get('key') or ''
        op = request.form.get('op')
        user = current_user()['email']
        today = paris_today()
        c = cx()
        try:
            V.ensure_table(c)
            row = c.execute('SELECT * FROM verification_items WHERE entity_id IS ? AND item_key=?', (_eid(), key)).fetchone()
            if not row:
                abort(404)
            if op == 'fait':
                f = _find(c, key)
                days = f['follow_up_days'] if f else 3
                V.record_action(c, _eid(), key, 'Traité depuis la page dédiée', 'déclaré traité par ' + user, user, today, days)
                flash(f"Noté. ProfitOS vérifiera le {(today + timedelta(days=days)).strftime('%d/%m/%Y')} que le problème a disparu.")
            elif op == 'reporter':
                V.snooze(c, _eid(), key, SNOOZE_DAYS, user, today)
                flash(f'Point reporté de {SNOOZE_DAYS} jours.')
            elif op == 'ecarter':
                reason = (request.form.get('reason') or '').strip()[:200]
                if not reason:
                    flash('Indiquez pourquoi ce point est écarté.')
                    return redirect(url_for('mission_control'))
                V.dismiss(c, _eid(), key, reason, user, today)
                log_activity('MISSION_CONTROL_DISMISS', f"{row['title']} : {reason}"[:300])
                flash('Point écarté.')
            elif op == 'retablir':
                V.restore(c, _eid(), key)
                flash('Point rétabli.')
            else:
                abort(400)
        finally:
            c.close()
        return redirect(url_for('mission_control'))

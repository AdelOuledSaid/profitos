"""Chaîne de traitement des factures fournisseurs (v486) : dix étapes par facture,
validation humaine, exécution, contrôle du résultat."""
from profitos.runtime import *
from profitos.feature_access import requires_paid_plan
from profitos import purchase_pipeline as PP
from profitos import verification as V
from profitos.accounting import paris_today


def _eid():
    from profitos.entities import current_entity_id
    return current_entity_id()


def _links(actions):
    out = []
    for a in actions:
        if a['kind'] == 'link':
            try:
                out.append(dict(a, url=url_for(a['endpoint'], **(a.get('args') or {}))))
            except Exception:
                pass
        else:
            out.append(a)
    return out


def register(app):

    @app.route('/facturation/achats/traitement', methods=['GET', 'POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_pipeline():
        today = paris_today()
        c = cx()
        try:
            if request.method == 'POST':
                ids = [int(x) for x in request.form.getlist('purchase_id') if x.isdigit()][:100]
                if not ids:
                    flash('Cochez au moins une facture prête à valider.')
                    return redirect(url_for('purchase_pipeline'))
                done, refused = 0, []
                for pid in ids:
                    ok, msg = PP.execute(c, _eid(), pid, 'approve', current_user()['email'], current_role(), today)
                    if ok:
                        done += 1
                    else:
                        refused.append(f"#{pid} : {msg}")
                log_activity('PURCHASE_PIPELINE_BATCH', f"{done} facture(s) validée(s) et comptabilisée(s) par la chaîne")
                flash(f"{done} facture(s) validée(s), comptabilisée(s) et contrôlée(s)." + (f" {len(refused)} refusée(s)." if refused else ''))
                for r in refused[:5]:
                    flash(r)
                return redirect(url_for('purchase_pipeline'))
            items = PP.queue(c, _eid(), today)
        finally:
            c.close()
        ready = [a for a in items if a['purchase'].get('validation_status') == 'pending' and
                 any(x['op'] == 'approve' and x['allowed'] for x in a['actions'])]
        groups = {'ready': ready,
                  'blocked': [a for a in items if a['level'] == V.BLOCKED],
                  'check': [a for a in items if a['level'] == V.TO_CHECK and a not in ready],
                  'ok': [a for a in items if a['level'] == V.READY and a not in ready]}
        return render_template('purchase_pipeline.html', groups=groups, total=len(items), step_names=PP.STEP_NAMES,
                               can_validate=current_role() in PP.VALIDATOR_ROLES)

    @app.route('/facturation/achats/<int:purchase_id>/traitement', methods=['GET', 'POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_pipeline_detail(purchase_id):
        today = paris_today()
        c = cx()
        try:
            if request.method == 'POST':
                ok, msg = PP.execute(c, _eid(), purchase_id, request.form.get('op') or '', current_user()['email'],
                                     current_role(), today)
                if ok:
                    log_activity('PURCHASE_PIPELINE_EXECUTE', f"Facture fournisseur #{purchase_id} : {msg}"[:300])
                flash(msg)
                return redirect(url_for('purchase_pipeline_detail', purchase_id=purchase_id))
            a = PP.analyse(c, _eid(), purchase_id, today)
        finally:
            c.close()
        if not a:
            abort(404)
        return render_template('purchase_pipeline_detail.html', a=a, actions=_links(a['actions']),
                               can_validate=current_role() in PP.VALIDATOR_ROLES)

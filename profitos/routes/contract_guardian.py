"""Contract Guardian (v481) : registre des contrats fournisseurs et contrôles des factures."""
from profitos.runtime import *
from profitos.feature_access import requires_paid_plan
from profitos import contract_guardian as CG
from profitos import verification as V
from profitos.accounting import paris_today


def _eid():
    from profitos.entities import current_entity_id
    return current_entity_id()


def _index_choices():
    from profitos import market_watch as MW
    return [(code, d['label']) for code, d in MW.CATALOGUE.items() if MW.is_price_index(dict(d, code=code))]


def _suppliers(c):
    try:
        return [dict(r) for r in c.execute('SELECT id, name FROM suppliers ORDER BY name').fetchall()]
    except Exception:
        return []


def _form_data(f):
    data = {k: f.get(k) for k in CG.FIELDS}
    data['tacit_renewal'] = f.get('tacit_renewal')
    for k in ('amount_ht', 'tolerance_pct', 'cap_pct'):
        if (data[k] or '').strip():
            v = parse_amount(data[k])
            if v is None:
                raise CG.ContractError('Montant ou pourcentage invalide.')
            data[k] = v
        else:
            data[k] = None
    return data


def register(app):

    @app.route('/contrats-fournisseurs')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def contract_guardian():
        from profitos.routes.mission_control import _decorate
        from profitos import mission_control as MC
        today = paris_today()
        c = cx()
        try:
            findings = CG.detect(c, _eid(), today)
            visible = [_decorate(f) for f in V.sync(c, _eid(), findings, today, None, set(CG.KINDS))]
            ks = [CG.contract_view(k, today) for k in CG.contracts(c, _eid())]
        finally:
            c.close()
        open_items = [f for f in visible if f['track']['status'] == V.OPEN]
        in_control = [f for f in visible if f['track']['status'] == V.IN_CONTROL]
        savings = round(sum(f['amount'] or 0 for f in open_items if f['kind'] == 'contrat_ecart' and f['level'] == V.READY), 2)
        return render_template('contract_guardian.html', contracts=ks, findings=open_items, in_control=in_control,
                               savings=savings, severity_labels=V.SEVERITY_LABELS, kind_labels=MC.KIND_LABELS,
                               today=today)

    def _form(contract=None, error=None, data=None):
        from profitos import market_watch as MW
        c = cx()
        try:
            suppliers = _suppliers(c)
        finally:
            c.close()
        values = data or contract or {'supplier_name': request.args.get('fournisseur', '')[:120], 'periodicity': 'mensuel',
                                      'tolerance_pct': 2, 'renewal_months': 12, 'notice_days': 0, 'status': 'actif'}
        return render_template('contract_form.html', contract=contract, values=values, error=error, suppliers=suppliers,
                               periodicities=CG.PERIODICITIES, indices=_index_choices()), (400 if error else 200)

    def _save(contract_id=None):
        try:
            data = _form_data(request.form)
            c = cx()
            try:
                if contract_id and not CG.get(c, _eid(), contract_id):
                    abort(404)
                CG.save(c, _eid(), data, now(), contract_id)
            finally:
                c.close()
        except (CG.ContractError, ValueError) as exc:
            existing = None
            if contract_id:
                c = cx()
                try:
                    existing = CG.get(c, _eid(), contract_id)
                finally:
                    c.close()
            return _form(existing, str(exc), request.form.to_dict())
        log_activity('SUPPLIER_CONTRACT_SAVED', f"Contrat fournisseur « {request.form.get('label', '')[:80]} » enregistré")
        flash('Contrat enregistré. Les factures du fournisseur sont contrôlées.')
        return redirect(url_for('contract_guardian'))

    @app.route('/contrats-fournisseurs/nouveau', methods=['GET', 'POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def contract_guardian_new():
        if request.method == 'POST':
            return _save()
        return _form()

    @app.route('/contrats-fournisseurs/<int:contract_id>', methods=['GET', 'POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def contract_guardian_edit(contract_id):
        if request.method == 'POST':
            return _save(contract_id)
        c = cx()
        try:
            k = CG.get(c, _eid(), contract_id)
        finally:
            c.close()
        if not k:
            abort(404)
        return _form(k)

    @app.post('/contrats-fournisseurs/<int:contract_id>/supprimer')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def contract_guardian_delete(contract_id):
        c = cx()
        try:
            k = CG.get(c, _eid(), contract_id)
            if not k:
                abort(404)
            CG.delete(c, _eid(), contract_id)
        finally:
            c.close()
        log_activity('SUPPLIER_CONTRACT_DELETED', f"Contrat fournisseur « {k['label'][:80]} » supprimé")
        flash('Contrat supprimé.')
        return redirect(url_for('contract_guardian'))

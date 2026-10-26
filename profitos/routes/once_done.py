"""Once & Done (v482) : dossier entreprise unique et démarches pré-remplies."""
from profitos.runtime import *
from profitos.feature_access import requires_paid_plan
from profitos import once_done as OD
from profitos import verification as V
from profitos.accounting import paris_today


def _eid():
    from profitos.entities import current_entity_id
    return current_entity_id()


def _owner_email():
    u = current_user()
    return u['email'] if u else None


def _docs(info):
    out = []
    for key, doc in OD.DOCUMENTS.items():
        checks, level = OD.readiness(key, info)
        out.append({'key': key, 'doc': doc, 'checks': checks, 'level': level, 'allowed': can_access(doc['area']),
                    'missing': [ch for ch in checks if ch['ok'] is not True]})
    return out


def register(app):

    @app.route('/demarches', methods=['GET', 'POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    def once_done():
        c = cx()
        try:
            if request.method == 'POST':
                if not can_access('settings'):
                    flash("Votre rôle ne permet pas de modifier le dossier de l'entreprise.")
                    return redirect(url_for('once_done'))
                errors = OD.save_once(c, _eid(), request.form, now(), current_user()['email'])
                if errors:
                    for e in errors:
                        flash(e)
                else:
                    log_activity('ONCE_DONE_SAVED', "Dossier entreprise complété")
                    flash('Enregistré une fois pour toutes : ces informations sont reprises dans vos démarches.')
                return redirect(url_for('once_done'))
            info = OD.dossier(c, _eid(), _owner_email())
        finally:
            c.close()
        counts = {lvl: sum(1 for v in info.values() if V.level([v['check']]) == lvl) for lvl in (V.READY, V.TO_CHECK, V.BLOCKED)}
        return render_template('once_done.html', info=info, docs=_docs(info), counts=counts,
                               can_edit=can_access('settings'))

    @app.route('/demarches/<doc_key>', methods=['GET', 'POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    def once_done_document(doc_key):
        if doc_key not in OD.DOCUMENTS:
            abort(404)
        doc = OD.DOCUMENTS[doc_key]
        if not can_access(doc['area']):
            flash("Votre rôle ne donne pas accès à cette démarche.")
            return redirect(url_for('once_done'))
        today = paris_today()
        c = cx()
        try:
            info = OD.dossier(c, _eid(), _owner_email())
            checks, level = OD.readiness(doc_key, info)
            choices = {}
            if doc_key == 'mise_en_demeure':
                choices['invoices'] = OD.overdue_invoices(c, _eid(), today)
            elif doc_key == 'resiliation':
                from profitos import contract_guardian as CG
                choices['contracts'] = [CG.contract_view(k, today) for k in CG.contracts(c, _eid()) if k['status'] == 'actif']
                choices['addresses'] = {}
                for k in choices['contracts']:
                    if k.get('supplier_id'):
                        row = c.execute('SELECT address FROM suppliers WHERE id=?', (k['supplier_id'],)).fetchone()
                        choices['addresses'][k['id']] = (row['address'] if row else '') or ''
                sel = request.args.get('contract_id')
                first = next((k for k in choices['contracts'] if str(k['id']) == sel), None) or \
                    (choices['contracts'][0] if choices['contracts'] else None)
                choices['default_address'] = choices['addresses'].get(first['id'], '') if first else ''
            if request.method == 'POST':
                if level != V.READY:
                    flash("Document non préparé : complétez d'abord les données marquées « ? » ou « ✗ ».")
                    return redirect(url_for('once_done_document', doc_key=doc_key))
                try:
                    ctx = OD.letter_context(c, _eid(), doc_key, info, request.form, today)
                except ValueError as exc:
                    flash(str(exc))
                    return redirect(url_for('once_done_document', doc_key=doc_key, **{k: v for k, v in request.form.items()
                                                                                     if k in ('invoice_id', 'contract_id')}))
                log_activity('ONCE_DONE_DOCUMENT', f"Démarche préparée : {doc['title']}")
                return render_template('once_done_letter.html', **ctx)
        finally:
            c.close()
        return render_template('once_done_document.html', doc_key=doc_key, doc=doc, checks=checks, level=level,
                               choices=choices, args=request.args, today=today)

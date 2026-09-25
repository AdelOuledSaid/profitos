from profitos.runtime import *
from profitos.feature_access import requires_paid_plan
from profitos.webhooks_outbound import validate_outbound_webhook_url, new_webhook_secret, deliver_webhook, WEBHOOK_EVENTS


def register(app):
    # ------------------------------------------------------------------
    # API publique en lecture seule, authentifiée par clé API (Bearer token).
    # Permet à un outil externe (ERP, logiciel de facturation...) de lire les
    # données ProfitOS d'une organisation. Écriture non disponible dans cette
    # première version — volontairement, pour limiter la surface de risque.
    # ------------------------------------------------------------------

    @app.route('/api/v1/recover')
    @api_key_required
    def api_recover():
        tc=tenant_cx_direct(g.api_org_id)
        rows=tc.execute("SELECT id,invoice_number,customer,MAX(amount-paid_amount,0) outstanding,days_overdue,status,score,kind FROM invoices WHERE LOWER(COALESCE(status,''))!='paid' AND days_overdue>0 ORDER BY score DESC").fetchall()
        tc.close()
        return jsonify({'data':[dict(r) for r in rows],'count':len(rows)})

    @app.route('/api/v1/save')
    @api_key_required
    def api_save():
        tc=tenant_cx_direct(g.api_org_id)
        rows=tc.execute("SELECT id,title,value,score,details FROM opportunities WHERE type='SAVE' AND status='OPEN' ORDER BY score DESC").fetchall()
        tc.close()
        return jsonify({'data':[dict(r) for r in rows],'count':len(rows)})

    @app.route('/api/v1/grow')
    @api_key_required
    def api_grow():
        tc=tenant_cx_direct(g.api_org_id)
        rows=tc.execute("SELECT id,title,buyer,score,departments,deadline FROM opportunities WHERE type='GROW' AND status='OPEN' ORDER BY score DESC").fetchall()
        tc.close()
        return jsonify({'data':[dict(r) for r in rows],'count':len(rows)})

    @app.route('/api/v1/summary')
    @api_key_required
    def api_summary():
        tc=tenant_cx_direct(g.api_org_id)
        recover=tc.execute("SELECT COALESCE(SUM(MAX(amount-paid_amount,0)),0) t FROM invoices WHERE LOWER(COALESCE(status,''))!='paid' AND days_overdue>0").fetchone()['t']
        save=tc.execute("SELECT COALESCE(SUM(value),0) t FROM opportunities WHERE type='SAVE' AND status='OPEN'").fetchone()['t']
        grow_n=tc.execute("SELECT COUNT(*) c FROM opportunities WHERE type='GROW' AND status='OPEN'").fetchone()['c']
        tc.close()
        return jsonify({'recover':recover,'save':save,'grow_opportunities':grow_n})

    # ------------------------------------------------------------------
    # API en écriture — nécessite une clé créée avec la portée explicite
    # 'read_write' (voir api_write_required dans profitos/runtime.py). Une
    # clé existante créée avant l'introduction de ce champ reste en lecture
    # seule par défaut, jamais élevée en silence.
    # ------------------------------------------------------------------

    @app.route('/api/v1/purchase-invoices', methods=['POST'])
    @api_key_required
    @api_write_required
    def api_create_purchase_invoice():
        from profitos.routes.invoicing import PURCHASE_CATEGORY_LABELS
        from profitos.accounting import generate_purchase_entry, AccountingError
        payload = request.get_json(silent=True) or {}
        supplier_name = (payload.get('supplier_name') or '').strip()
        invoice_number = (payload.get('invoice_number') or '').strip()
        try:
            subtotal = float(payload.get('subtotal'))
            vat_amount = float(payload.get('vat_amount', 0) or 0)
        except (TypeError, ValueError):
            return jsonify({'error': 'invalid_amount', 'message': 'subtotal doit être un nombre.'}), 400
        if not supplier_name or not invoice_number:
            return jsonify({'error': 'missing_fields', 'message': 'supplier_name et invoice_number sont obligatoires.'}), 400
        category = payload.get('category', 'autre')
        if category not in PURCHASE_CATEGORY_LABELS:
            category = 'autre'
        total = round(subtotal + vat_amount, 2)

        tc = tenant_cx_direct(g.api_org_id)
        existing = tc.execute('SELECT id FROM purchase_invoices WHERE invoice_number=?', (invoice_number,)).fetchone()
        if existing:
            tc.close()
            return jsonify({'error': 'duplicate_invoice_number', 'message': f"Facture {invoice_number} déjà enregistrée."}), 409
        tc.execute(
            """INSERT INTO purchase_invoices
               (supplier_name,invoice_number,issue_date,due_date,subtotal,vat_amount,total,status,notes,created_at,category,validation_status)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (supplier_name, invoice_number, payload.get('issue_date'), payload.get('due_date'),
             subtotal, vat_amount, total, 'unpaid', 'Créée via API', now(), category, 'pending'),
        )
        tc.commit()
        new_id = tc.execute('SELECT last_insert_rowid()').fetchone()[0]
        try:
            row = tc.execute('SELECT * FROM purchase_invoices WHERE id=?', (new_id,)).fetchone()
            generate_purchase_entry(tc, row)
        except AccountingError as e:
            log_ops_event('ACCOUNTING_ENTRY_FAILED', outcome='ERROR', detail=f"achat API {new_id}: {e}")
        tc.close()
        log_activity('API_PURCHASE_CREATED', f"Facture fournisseur {invoice_number} créée via API")
        return jsonify({'id': new_id, 'invoice_number': invoice_number, 'total': total, 'validation_status': 'pending'}), 201

    @app.route('/api/v1/expense-reports', methods=['POST'])
    @api_key_required
    @api_write_required
    def api_create_expense_report():
        from profitos.expenses import EXPENSE_REPORT_CATEGORY_LABELS
        payload = request.get_json(silent=True) or {}
        employee_email = (payload.get('employee_email') or '').strip()
        lines = payload.get('lines') or []
        if not employee_email:
            return jsonify({'error': 'missing_fields', 'message': 'employee_email est obligatoire.'}), 400
        if not lines:
            return jsonify({'error': 'missing_lines', 'message': 'Au moins une ligne de dépense est requise.'}), 400
        clean_lines = []
        for i, l in enumerate(lines):
            category = l.get('category', 'autre')
            if category not in EXPENSE_REPORT_CATEGORY_LABELS:
                category = 'autre'
            try:
                amount = float(l.get('amount'))
            except (TypeError, ValueError):
                return jsonify({'error': 'invalid_amount', 'message': f"Ligne {i+1} : amount invalide."}), 400
            if amount <= 0:
                return jsonify({'error': 'invalid_amount', 'message': f"Ligne {i+1} : amount doit être positif."}), 400
            clean_lines.append({
                'category': category, 'amount': amount,
                'expense_date': l.get('expense_date'), 'description': l.get('description', ''),
            })

        tc = tenant_cx_direct(g.api_org_id)
        tc.execute(
            "INSERT INTO expense_reports(employee_email,period_label,status,created_at) VALUES(?,?,'draft',?)",
            (employee_email, payload.get('period_label'), now()),
        )
        tc.commit()
        report_id = tc.execute('SELECT last_insert_rowid()').fetchone()[0]
        for l in clean_lines:
            tc.execute(
                "INSERT INTO expense_report_lines(report_id,expense_date,category,description,amount,created_at) VALUES(?,?,?,?,?,?)",
                (report_id, l['expense_date'], l['category'], l['description'], l['amount'], now()),
            )
        tc.commit(); tc.close()
        log_activity('API_EXPENSE_REPORT_CREATED', f"Note de frais #{report_id} créée via API pour {employee_email}")
        return jsonify({'id': report_id, 'employee_email': employee_email, 'status': 'draft',
                         'lines_count': len(clean_lines)}), 201

    @app.route('/api/v1/invoices', methods=['POST'])
    @api_key_required
    @api_write_required
    def api_create_invoice():
        payload = request.get_json(silent=True) or {}
        client_name = (payload.get('client_name') or '').strip()
        lines = payload.get('lines') or []
        if not client_name:
            return jsonify({'error': 'missing_fields', 'message': 'client_name est obligatoire.'}), 400
        if not lines:
            return jsonify({'error': 'missing_lines', 'message': 'Au moins une ligne de facture est requise.'}), 400
        subtotal = 0.0
        vat_amount = 0.0
        clean_lines = []
        for i, l in enumerate(lines):
            try:
                qty = float(l.get('qty', 1))
                price = float(l.get('unit_price'))
                vat_rate = float(l.get('vat_rate', 20))
            except (TypeError, ValueError):
                return jsonify({'error': 'invalid_line', 'message': f"Ligne {i+1} : qty/unit_price/vat_rate invalide."}), 400
            line_ht = round(qty * price, 2)
            subtotal += line_ht
            vat_amount += round(line_ht * vat_rate / 100, 2)
            clean_lines.append({'label': l.get('label', ''), 'qty': qty, 'unit_price': price, 'vat_rate': vat_rate})
        subtotal = round(subtotal, 2)
        vat_amount = round(vat_amount, 2)
        total = round(subtotal + vat_amount, 2)

        tc = tenant_cx_direct(g.api_org_id)
        seq = tc.execute("SELECT COUNT(*) n FROM outgoing_invoices").fetchone()['n'] + 1
        invoice_number = f"FA-API-{date.today().year}-{seq:04d}"
        tc.execute(
            """INSERT INTO outgoing_invoices
               (invoice_number,client_name,client_address,client_email,issue_date,due_date,
                line_items,subtotal,vat_amount,total,status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (invoice_number, client_name, payload.get('client_address', ''), payload.get('client_email', ''),
             date.today().isoformat(), payload.get('due_date'), json.dumps(clean_lines, ensure_ascii=False),
             subtotal, vat_amount, total, 'draft', now()),
        )
        tc.commit()
        new_id = tc.execute('SELECT last_insert_rowid()').fetchone()[0]
        tc.close()
        log_activity('API_INVOICE_CREATED', f"Facture {invoice_number} créée via API (brouillon)")
        return jsonify({'id': new_id, 'invoice_number': invoice_number, 'total': total, 'status': 'draft'}), 201

    # ------------------------------------------------------------------
    # API en lecture étendue — relire ce que l'API en écriture permet de
    # créer, plus les objets tiers (fournisseurs, entités). Même clé, même
    # portée 'read' minimale — ces routes n'exposent jamais plus qu'un
    # simple accès en lecture ne devrait.
    # ------------------------------------------------------------------

    @app.route('/api/v1/purchase-invoices', methods=['GET'])
    @api_key_required
    def api_list_purchase_invoices():
        tc = tenant_cx_direct(g.api_org_id)
        status = request.args.get('status')
        try:
            limit = min(int(request.args.get('limit', 50)), 200)
        except (TypeError, ValueError):
            limit = 50
        query = "SELECT id,supplier_name,invoice_number,issue_date,due_date,total,status,validation_status,entity_id FROM purchase_invoices"
        params = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = tc.execute(query, params).fetchall()
        tc.close()
        return jsonify({'purchase_invoices': [dict(r) for r in rows]})

    @app.route('/api/v1/purchase-invoices/<int:purchase_id>', methods=['GET'])
    @api_key_required
    def api_get_purchase_invoice(purchase_id):
        tc = tenant_cx_direct(g.api_org_id)
        row = tc.execute("SELECT * FROM purchase_invoices WHERE id=?", (purchase_id,)).fetchone()
        tc.close()
        if not row:
            return jsonify({'error': 'not_found', 'message': f"Facture fournisseur {purchase_id} introuvable."}), 404
        return jsonify(dict(row))

    @app.route('/api/v1/purchase-invoices/<int:purchase_id>/mark-paid', methods=['POST'])
    @api_key_required
    @api_write_required
    def api_mark_purchase_paid(purchase_id):
        from profitos.accounting import generate_purchase_payment_entry, AccountingError
        tc = tenant_cx_direct(g.api_org_id)
        row = tc.execute("SELECT * FROM purchase_invoices WHERE id=?", (purchase_id,)).fetchone()
        if not row:
            tc.close()
            return jsonify({'error': 'not_found', 'message': f"Facture fournisseur {purchase_id} introuvable."}), 404
        if row['status'] == 'paid':
            tc.close()
            return jsonify({'error': 'already_paid', 'message': 'Cette facture est déjà marquée payée.'}), 409
        tc.execute("UPDATE purchase_invoices SET status='paid',paid_at=? WHERE id=?", (now(), purchase_id))
        tc.commit()
        updated = tc.execute("SELECT * FROM purchase_invoices WHERE id=?", (purchase_id,)).fetchone()
        try:
            generate_purchase_payment_entry(tc, updated)
        except AccountingError as e:
            log_ops_event('ACCOUNTING_ENTRY_FAILED', outcome='ERROR', detail=f"règlement API achat {purchase_id}: {e}")
        tc.close()
        log_activity('API_PURCHASE_MARKED_PAID', f"Facture fournisseur {purchase_id} marquée payée via API")
        return jsonify({'id': purchase_id, 'status': 'paid'})

    @app.route('/api/v1/invoices', methods=['GET'])
    @api_key_required
    def api_list_invoices():
        tc = tenant_cx_direct(g.api_org_id)
        status = request.args.get('status')
        try:
            limit = min(int(request.args.get('limit', 50)), 200)
        except (TypeError, ValueError):
            limit = 50
        query = "SELECT id,invoice_number,client_name,issue_date,due_date,total,status,entity_id FROM outgoing_invoices"
        params = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = tc.execute(query, params).fetchall()
        tc.close()
        return jsonify({'invoices': [dict(r) for r in rows]})

    @app.route('/api/v1/invoices/<int:invoice_id>', methods=['GET'])
    @api_key_required
    def api_get_invoice(invoice_id):
        tc = tenant_cx_direct(g.api_org_id)
        row = tc.execute("SELECT * FROM outgoing_invoices WHERE id=?", (invoice_id,)).fetchone()
        tc.close()
        if not row:
            return jsonify({'error': 'not_found', 'message': f"Facture {invoice_id} introuvable."}), 404
        data = dict(row)
        try:
            data['line_items'] = json.loads(data.get('line_items') or '[]')
        except (TypeError, ValueError):
            pass
        return jsonify(data)

    @app.route('/api/v1/suppliers', methods=['GET'])
    @api_key_required
    def api_list_suppliers():
        tc = tenant_cx_direct(g.api_org_id)
        rows = tc.execute(
            "SELECT id,name,email,phone,siret,vat_number,iban,bic FROM suppliers ORDER BY name"
        ).fetchall()
        tc.close()
        return jsonify({'suppliers': [dict(r) for r in rows]})

    @app.route('/api/v1/suppliers', methods=['POST'])
    @api_key_required
    @api_write_required
    def api_create_supplier():
        from profitos.sepa import validate_iban
        payload = request.get_json(silent=True) or {}
        name = (payload.get('name') or '').strip()
        if not name:
            return jsonify({'error': 'missing_fields', 'message': 'name est obligatoire.'}), 400
        iban = (payload.get('iban') or '').replace(' ', '').upper().strip()
        if iban and not validate_iban(iban):
            return jsonify({'error': 'invalid_iban', 'message': f"IBAN invalide : {payload.get('iban')!r}."}), 400
        tc = tenant_cx_direct(g.api_org_id)
        tc.execute(
            """INSERT INTO suppliers(name,email,phone,address,siret,vat_number,notes,iban,bic,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (name, (payload.get('email') or '').strip(), (payload.get('phone') or '').strip(),
             (payload.get('address') or '').strip(), (payload.get('siret') or '').strip(),
             (payload.get('vat_number') or '').strip(), (payload.get('notes') or '').strip(),
             iban, (payload.get('bic') or '').upper().strip(), now()),
        )
        tc.commit()
        new_id = tc.execute('SELECT last_insert_rowid()').fetchone()[0]
        tc.close()
        log_activity('API_SUPPLIER_CREATED', f"Fournisseur « {name} » créé via API")
        return jsonify({'id': new_id, 'name': name}), 201

    @app.route('/api/v1/entities', methods=['GET'])
    @api_key_required
    def api_list_entities():
        from profitos.entities import list_all_entities
        tc = tenant_cx_direct(g.api_org_id)
        entities = list_all_entities(tc)
        tc.close()
        return jsonify({'entities': entities})

    # ------------------------------------------------------------------
    # Gestion des clés API (créer / lister / révoquer) — page normale,
    # authentifiée par session comme le reste de l'app, pas par clé API.
    # ------------------------------------------------------------------

    @app.route('/settings/api-keys',methods=['GET','POST'])
    @login_required
    @require_area('settings')
    @requires_paid_plan
    def api_keys():
        org=current_org()
        if current_role() not in ('OWNER','ADMIN'):
            flash("Seuls le propriétaire ou un administrateur peuvent gérer les clés API.")
            return redirect(url_for('settings'))
        c=auth_cx()
        if request.method=='POST':
            action=request.form.get('action')
            if action=='create':
                raw_key=generate_api_key()
                scope=request.form.get('scope') if request.form.get('scope') in ('read','read_write') else 'read'
                c.execute('INSERT INTO api_keys(organization_id,key_hash,key_prefix,created_by,created_at,scope) VALUES(?,?,?,?,?,?)',
                    (org['id'],hash_api_key(raw_key),raw_key[:16],current_user()['email'],now(),scope))
                c.commit(); c.close()
                log_activity('API_KEY_CREATED',f'Nouvelle clé API créée (portée : {scope})')
                flash('Clé créée — copie-la maintenant, elle ne sera plus jamais affichée en clair.')
                keys=_load_api_keys(org['id'])
                return render_template('api_keys.html',keys=keys,new_key=raw_key)
            elif action=='revoke':
                kid=request.form.get('key_id')
                c.execute('UPDATE api_keys SET revoked_at=? WHERE id=? AND organization_id=?',(now(),kid,org['id']))
                c.commit(); c.close()
                log_activity('API_KEY_REVOKED',f'Clé API #{kid} révoquée')
                flash('Clé révoquée.')
                return redirect(url_for('api_keys'))
        c.close()
        keys=_load_api_keys(org['id'])
        return render_template('api_keys.html',keys=keys,new_key=None)

    # ------------------------------------------------------------------
    # Webhooks sortants : ProfitOS notifie une URL choisie par le client
    # quand un événement métier se produit (facture envoyée/payée, achat
    # créé/payé). Voir profitos/webhooks_outbound.py pour le moteur de
    # livraison, la signature HMAC et la protection SSRF.
    # ------------------------------------------------------------------

    @app.route('/settings/webhooks', methods=['GET', 'POST'])
    @login_required
    @require_area('settings')
    def outbound_webhooks():
        c = cx()
        error = None
        if request.method == 'POST':
            url = (request.form.get('url') or '').strip()
            events = request.form.getlist('events')
            ok, reason = validate_outbound_webhook_url(url)
            if not ok:
                error = reason
            elif not events:
                error = "Sélectionne au moins un événement."
            else:
                secret = new_webhook_secret()
                c.execute(
                    'INSERT INTO webhook_subscriptions(url,secret,events,is_active,created_at,created_by) VALUES(?,?,?,1,?,?)',
                    (url, secret, ','.join(events), now(), current_user()['email']),
                )
                c.commit()
                log_activity('WEBHOOK_CREATED', f"Webhook sortant créé vers {url}")
                flash(f"Webhook créé. Secret de signature (copie-le maintenant, affiché une seule fois) : {secret}")
                return redirect(url_for('outbound_webhooks'))

        subs = c.execute('SELECT * FROM webhook_subscriptions ORDER BY id DESC').fetchall()
        recent_deliveries = {}
        for s in subs:
            recent_deliveries[s['id']] = c.execute(
                'SELECT * FROM webhook_deliveries WHERE subscription_id=? ORDER BY id DESC LIMIT 5',
                (s['id'],),
            ).fetchall()
        c.close()
        return render_template('webhooks_outbound.html', subs=subs, error=error,
                                events=WEBHOOK_EVENTS, recent_deliveries=recent_deliveries)

    @app.route('/settings/webhooks/<int:sub_id>/supprimer', methods=['POST'])
    @login_required
    @require_area('settings')
    def outbound_webhook_delete(sub_id):
        c = cx()
        c.execute('DELETE FROM webhook_subscriptions WHERE id=?', (sub_id,))
        c.execute('DELETE FROM webhook_deliveries WHERE subscription_id=?', (sub_id,))
        c.commit(); c.close()
        flash("Webhook supprimé.")
        return redirect(url_for('outbound_webhooks'))

    @app.route('/settings/webhooks/<int:sub_id>/tester', methods=['POST'])
    @login_required
    @require_area('settings')
    def outbound_webhook_test(sub_id):
        c = cx()
        sub = c.execute('SELECT * FROM webhook_subscriptions WHERE id=?', (sub_id,)).fetchone()
        if not sub:
            c.close(); abort(404)
        events = (sub['events'] or '').split(',')
        test_event = events[0] if events else 'invoice.sent'
        deliver_webhook(c, test_event, {'test': True, 'message': 'Ceci est un envoi de test depuis ProfitOS.'},
                         only_subscription_id=sub_id)
        c.close()
        flash(f"Test envoyé ({test_event}) — regarde le journal des livraisons ci-dessous pour le résultat.")
        return redirect(url_for('outbound_webhooks'))


def _load_api_keys(org_id):
    c=auth_cx()
    rows=c.execute('SELECT * FROM api_keys WHERE organization_id=? ORDER BY id DESC',(org_id,)).fetchall()
    c.close()
    return rows

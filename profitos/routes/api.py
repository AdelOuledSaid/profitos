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
                c.execute('INSERT INTO api_keys(organization_id,key_hash,key_prefix,created_by,created_at) VALUES(?,?,?,?,?)',
                    (org['id'],hash_api_key(raw_key),raw_key[:16],current_user()['email'],now()))
                c.commit(); c.close()
                log_activity('API_KEY_CREATED','Nouvelle clé API créée')
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

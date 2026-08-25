from profitos.runtime import *
from profitos.feature_access import requires_paid_plan


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


def _load_api_keys(org_id):
    c=auth_cx()
    rows=c.execute('SELECT * FROM api_keys WHERE organization_id=? ORDER BY id DESC',(org_id,)).fetchall()
    c.close()
    return rows

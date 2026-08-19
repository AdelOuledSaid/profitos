from profitos.runtime import *



def register(app):
    @app.route('/')
    @login_required
    @requires_active_plan
    def home():
        c=cx(); recover=c.execute("SELECT COALESCE(SUM(MAX(amount-paid_amount,0)),0) t FROM invoices WHERE LOWER(COALESCE(status,''))!='paid' AND days_overdue>0").fetchone()['t']; save=c.execute("SELECT COALESCE(SUM(value),0) t FROM opportunities WHERE type='SAVE' AND status='OPEN'").fetchone()['t']; grow=c.execute("SELECT COUNT(*) c FROM opportunities WHERE type='GROW' AND status='OPEN'").fetchone()['c']; pending=c.execute("SELECT COUNT(*) c FROM actions WHERE status='PENDING'").fetchone()['c']; verified=c.execute("SELECT COALESCE(SUM(amount),0) t FROM outcomes WHERE verified=1").fetchone()['t']
        top=list(c.execute("SELECT id,invoice_number title,customer subtitle,MAX(amount-paid_amount,0) value,score,'RECOVER' type FROM invoices WHERE LOWER(COALESCE(status,''))!='paid' AND days_overdue>0 ORDER BY score DESC LIMIT 3").fetchall())+list(c.execute("SELECT id,title,'' subtitle,value,score,'SAVE' type FROM opportunities WHERE type='SAVE' AND status='OPEN' ORDER BY score DESC LIMIT 2").fetchall())+list(c.execute("SELECT id,title,buyer subtitle,0 value,score,'GROW' type FROM opportunities WHERE type='GROW' AND status='OPEN' ORDER BY score DESC LIMIT 3").fetchall())
        snaps=c.execute('SELECT * FROM dso_snapshots ORDER BY snapshot_date ASC LIMIT 30').fetchall(); c.close(); top.sort(key=lambda x:x['score'],reverse=True)
        dso_values=[s['avg_days_overdue'] or 0 for s in snaps][-12:]
        dso_svg=sparkline_svg(dso_values) if len(dso_values)>=2 else None
        dso_current=round(dso_values[-1]) if dso_values else None
        dso_delta=round(dso_values[-1]-dso_values[-2]) if len(dso_values)>=2 else None
        # Un rôle ne voit que les modules auxquels il a accès (ex. un Commercial ne voit pas RECOVER/SAVE).
        top=[r for r in top if can_access(KIND_TO_AREA.get(r['type'],''))]
        return render_template('dashboard.html',
            recover=recover if can_access('recover') else None,
            save=save if can_access('save') else None,
            grow=grow if can_access('grow') else None,
            pending=pending,verified=verified,top=top[:6],
            dso_svg=dso_svg if can_access('recover') else None,dso_current=dso_current,dso_delta=dso_delta)

    @app.route('/company',methods=['GET','POST'])
    @login_required
    def company():
        c=cx()
        if request.method=='POST':
            if not can_access('settings'):
                c.close(); flash('Seuls le propriétaire ou un administrateur peuvent modifier le profil entreprise.'); return redirect(url_for('company'))
            dep=request.form.get('department','').strip(); allowed=request.form.get('allowed_departments','').strip() or dep
            vals=(request.form.get('name','').strip(),request.form.get('city','').strip(),dep,allowed,request.form.get('activities','').strip(),request.form.get('certifications','').strip(),now())
            c.execute('''INSERT INTO company(id,name,city,department,allowed_departments,activities,certifications,updated_at) VALUES(1,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,city=excluded.city,department=excluded.department,allowed_departments=excluded.allowed_departments,activities=excluded.activities,certifications=excluded.certifications,updated_at=excluded.updated_at''',vals); c.commit(); c.close(); flash('Profil entreprise enregistré.')
            try:flash(f'GROW actualisé : {sync_grow()} opportunités pertinentes.')
            except Exception as e:flash(f'Profil enregistré, mais BOAMP est indisponible : {e}')
            return redirect(url_for('grow'))
        p=c.execute('SELECT * FROM company WHERE id=1').fetchone(); c.close(); return render_template('company.html',p=p)

    @app.route('/recover')
    @login_required
    @requires_active_plan
    @require_area('recover')
    def recover():
        c=cx()
        active=c.execute("SELECT *,MAX(amount-paid_amount,0) outstanding FROM invoices WHERE LOWER(COALESCE(status,''))!='paid' AND days_overdue>0 ORDER BY score DESC").fetchall()
        total=sum(x['outstanding'] for x in active)
        filt=request.args.get('filter','all')
        if filt=='overdue':
            rows=[r for r in active if r['kind']!='RETENTION']
        elif filt=='retention':
            # Toutes les retenues (déjà libérables ET à venir) pour visibilité/planification,
            # pas seulement celles déjà comptées dans le total "recoverable".
            rows=c.execute("SELECT *,MAX(amount-paid_amount,0) outstanding FROM invoices WHERE kind='RETENTION' AND LOWER(COALESCE(status,''))!='paid' ORDER BY COALESCE(retention_release_date,due_date) ASC").fetchall()
        else:
            rows=active
        c.close()

        # Recherche/filtre avancé : client, montant min/max, période (échéance).
        q=request.args.get('q','').strip()
        min_amount=request.args.get('min_amount','').strip()
        max_amount=request.args.get('max_amount','').strip()
        date_from=request.args.get('date_from','').strip()
        date_to=request.args.get('date_to','').strip()
        if q:
            qn=norm(q)
            rows=[r for r in rows if qn in norm(r['customer']) or qn in norm(r['invoice_number'])]
        if min_amount:
            try: mn=float(min_amount); rows=[r for r in rows if r['outstanding']>=mn]
            except ValueError: pass
        if max_amount:
            try: mx=float(max_amount); rows=[r for r in rows if r['outstanding']<=mx]
            except ValueError: pass
        if date_from:
            rows=[r for r in rows if r['due_date'] and r['due_date']>=date_from]
        if date_to:
            rows=[r for r in rows if r['due_date'] and r['due_date']<=date_to]

        return render_template('recover.html',rows=rows,total=total,filt=filt,today=date.today().isoformat(),
            q=q,min_amount=min_amount,max_amount=max_amount,date_from=date_from,date_to=date_to)

    @app.route('/save')
    @login_required
    @requires_active_plan
    @require_area('save')
    def save():
        c=cx(); rows=c.execute("SELECT * FROM opportunities WHERE type='SAVE' AND status='OPEN' ORDER BY score DESC").fetchall(); total=sum(x['value'] for x in rows); c.close(); return render_template('save.html',rows=rows,total=total)

    @app.route('/grow')
    @login_required
    @requires_active_plan
    @require_area('grow')
    def grow():
        p=profile()
        if not p:return render_template('grow.html',rows=[],needs_profile=True,last=None,jlist=jlist,fmt_deadline=fmt_deadline,days_left=days_left)
        c=cx(); rows=c.execute("SELECT * FROM opportunities WHERE type='GROW' AND status='OPEN' ORDER BY score DESC").fetchall(); last=c.execute("SELECT * FROM audit_runs WHERE run_type='GROW' ORDER BY id DESC LIMIT 1").fetchone(); c.close(); return render_template('grow.html',rows=rows,needs_profile=False,last=last,jlist=jlist,fmt_deadline=fmt_deadline,days_left=days_left)

    @app.route('/grow/refresh',methods=['POST'])
    @login_required
    @require_area('grow')
    @rate_limit(6,300)
    def grow_refresh():
        try:flash(f'BOAMP actualisé : {sync_grow()} opportunités pertinentes.')
        except Exception as e:flash(f'Impossible d’actualiser BOAMP : {e}')
        return redirect(url_for('grow'))

    @app.route('/opportunity/<kind>/<int:item_id>')
    @login_required
    @requires_active_plan
    def detail(kind,item_id):
        kind=kind.upper()
        if not can_access(KIND_TO_AREA.get(kind,'')):
            flash("Votre rôle ne donne pas accès à cette section."); return redirect(url_for('home'))
        c=cx()
        if kind=='RECOVER':
            r=c.execute('SELECT *,MAX(amount-paid_amount,0) outstanding FROM invoices WHERE id=?',(item_id,)).fetchone()
            if not r:abort(404)
            if r['kind']=='RETENTION':
                release=r['retention_release_date']
                if release and release<=date.today().isoformat():
                    reasons=[f"Retenue de garantie libérable depuis le {release}",'contactez le client pour en demander la levée']
                elif release:
                    reasons=[f"Retenue de garantie libérable le {release}",'pas encore actionnable — visible pour anticipation']
                else:
                    reasons=['Retenue de garantie sans date de libération connue — à clarifier avec le contrat']
                o=dict(r); o.update(kind='RECOVER',title=f"Retenue de garantie — Facture #{r['invoice_number']}",value=r['outstanding'],reasons=reasons,warnings=['la retenue de garantie suit un régime contractuel différent d\'une facture standard'])
            else:
                o=dict(r); o.update(kind='RECOVER',title=f"Facture #{r['invoice_number']}",value=r['outstanding'],reasons=[f"{r['days_overdue']} jours de retard",'montant calculé depuis les données importées'],warnings=[])
        else:
            r=c.execute('SELECT * FROM opportunities WHERE id=? AND type=?',(item_id,kind)).fetchone()
            if not r:abort(404)
            o=dict(r); o.update(kind=kind,reasons=json.loads(r['reasons'] or '[]'),warnings=json.loads(r['warnings'] or '[]'),departments=jlist(r['departments']),deadline_human=fmt_deadline(r['deadline']),days_remaining=days_left(r['deadline']))
        acts=c.execute('SELECT * FROM actions WHERE opportunity_id=? AND kind=? ORDER BY id DESC',(item_id,kind)).fetchall(); dce=c.execute('SELECT * FROM dce_documents WHERE opportunity_id=? ORDER BY id DESC',(item_id,)).fetchall() if kind=='GROW' else []
        history=c.execute('SELECT * FROM status_history WHERE kind=? AND entity_id=? ORDER BY id DESC',(kind,item_id)).fetchall()
        c.close(); dce_items=[]
        for d in dce:
            x=dict(d); x['analysis']=json.loads(d['analysis_json'] or '{}'); dce_items.append(x)
        return render_template('detail.html',o=o,actions=acts,dce_items=dce_items,history=history)

    @app.route('/opportunity/<kind>/<int:item_id>/status',methods=['POST'])
    @login_required
    def opportunity_status(kind,item_id):
        kind=kind.upper()
        if not can_access(KIND_TO_AREA.get(kind,'')):
            flash("Votre rôle ne donne pas accès à cette section."); return redirect(url_for('home'))
        new_status=request.form.get('status','').strip()
        if not new_status: return redirect(url_for('detail',kind=kind,item_id=item_id))
        c=cx()
        if kind=='RECOVER':
            row=c.execute('SELECT * FROM invoices WHERE id=?',(item_id,)).fetchone()
            if not row: c.close(); abort(404)
            old=row['status']; c.execute('UPDATE invoices SET status=? WHERE id=?',(new_status,item_id)); c.commit()
            entity_type='INVOICE'
        else:
            row=c.execute('SELECT * FROM opportunities WHERE id=? AND type=?',(item_id,kind)).fetchone()
            if not row: c.close(); abort(404)
            old=row['status']; c.execute('UPDATE opportunities SET status=? WHERE id=?',(new_status,item_id)); c.commit()
            entity_type='OPPORTUNITY'
        c.close()
        log_status_change(entity_type,item_id,kind,old,new_status)
        log_activity('STATUS_CHANGE',f'{kind} #{item_id} : {old} → {new_status}')
        flash('Statut mis à jour.')
        return redirect(url_for('detail',kind=kind,item_id=item_id))


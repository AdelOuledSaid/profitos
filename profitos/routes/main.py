from profitos.runtime import *
from profitos.plan_limits import feature_enabled, PLAN_LIMITS
from profitos.feature_access import requires_feature, requires_paid_plan, current_plan_is_paid, _deny_paid_feature
from profitos.plan_usage import quota_state, record_usage
import io



def register(app):
    @app.route('/')
    def home():
        if not session.get('user_id'):
            return render_template('landing.html')
        return _home_dashboard()

    @requires_active_plan
    def _home_dashboard():
        c=cx(); recover=c.execute("SELECT COALESCE(SUM(MAX(amount-paid_amount,0)),0) t FROM invoices WHERE LOWER(COALESCE(status,''))!='paid' AND days_overdue>0").fetchone()['t']; save=c.execute("SELECT COALESCE(SUM(value),0) t FROM opportunities WHERE type='SAVE' AND status='OPEN'").fetchone()['t']; grow=c.execute("SELECT COUNT(*) c FROM opportunities WHERE type='GROW' AND status='OPEN'").fetchone()['c']; pending=c.execute("SELECT COUNT(*) c FROM actions WHERE status='PENDING'").fetchone()['c']; verified=c.execute("SELECT COALESCE(SUM(amount),0) t FROM outcomes WHERE verified=1").fetchone()['t']
        top=list(c.execute("SELECT id,invoice_number title,customer subtitle,MAX(amount-paid_amount,0) value,score,'RECOVER' type FROM invoices WHERE LOWER(COALESCE(status,''))!='paid' AND days_overdue>0 ORDER BY score DESC LIMIT 3").fetchall())+list(c.execute("SELECT id,title,'' subtitle,value,score,'SAVE' type FROM opportunities WHERE type='SAVE' AND status='OPEN' ORDER BY score DESC LIMIT 2").fetchall())+list(c.execute("SELECT id,title,buyer subtitle,0 value,score,'GROW' type FROM opportunities WHERE type='GROW' AND status='OPEN' ORDER BY score DESC LIMIT 3").fetchall())
        snaps=c.execute('SELECT * FROM dso_snapshots ORDER BY snapshot_date ASC LIMIT 30').fetchall(); c.close(); top.sort(key=lambda x:x['score'],reverse=True)
        dso_values=[s['avg_days_overdue'] or 0 for s in snaps][-12:]
        dso_svg=sparkline_svg(dso_values) if len(dso_values)>=2 else None
        dso_current=round(dso_values[-1]) if dso_values else None
        dso_delta=round(dso_values[-1]-dso_values[-2]) if len(dso_values)>=2 else None
        try: sector_benchmark=sector_dso_benchmark(session['org_id']) if dso_current is not None else None
        except Exception: sector_benchmark=None
        # Un rôle ne voit que les modules auxquels il a accès (ex. un Commercial ne voit pas RECOVER/SAVE).
        top=[r for r in top if can_access(KIND_TO_AREA.get(r['type'],''))]

        # Checklist d'onboarding — calculée à la volée depuis les données existantes,
        # pas de table dédiée : toujours synchronisée avec la réalité.
        c2=cx()
        has_company=bool(c2.execute("SELECT 1 FROM company WHERE id=1 AND name IS NOT NULL AND name!=''").fetchone())
        has_invoices=bool(c2.execute("SELECT 1 FROM invoices LIMIT 1").fetchone())
        has_expenses=bool(c2.execute("SELECT 1 FROM expenses LIMIT 1").fetchone())
        has_action=bool(c2.execute("SELECT 1 FROM actions LIMIT 1").fetchone())
        c2.close()
        ac2=auth_cx(); team_count=ac2.execute('SELECT COUNT(*) c FROM memberships WHERE organization_id=?',(session['org_id'],)).fetchone()['c']; ac2.close()
        onboarding_steps=[
            {'label':'Profil entreprise complété','done':has_company,'url':url_for('company')},
            {'label':'Premières factures importées','done':has_invoices,'url':url_for('upload_invoices')},
            {'label':'Premières dépenses importées','done':has_expenses,'url':url_for('upload_expenses')},
            {'label':'Première action préparée','done':has_action,'url':url_for('actions')},
            {'label':'Un collègue invité','done':team_count>1,'url':url_for('team')},
        ]
        onboarding_done=sum(1 for s in onboarding_steps if s['done'])
        show_onboarding=onboarding_done<len(onboarding_steps)

        return render_template('dashboard.html',
            recover=recover if can_access('recover') else None,
            save=save if can_access('save') else None,
            grow=grow if can_access('grow') else None,
            pending=pending,verified=verified,top=top[:6],
            dso_svg=dso_svg if can_access('recover') else None,dso_current=dso_current,dso_delta=dso_delta,
            sector_benchmark=sector_benchmark if can_access('recover') else None,
            onboarding_steps=onboarding_steps,onboarding_done=onboarding_done,show_onboarding=show_onboarding)

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
            org=current_org()
            if org and feature_enabled(org['plan'],'advanced_features'):
                try:flash(f'GROW actualisé : {sync_grow()} opportunités pertinentes.')
                except Exception as e:flash(f'Profil enregistré, mais BOAMP est indisponible : {e}')
            return redirect(url_for('grow'))
        p=c.execute('SELECT * FROM company WHERE id=1').fetchone(); c.close(); return render_template('company.html',p=p)

    @app.route('/margin-watch',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @require_area('save')
    def margin_watch():
        """Suivi manuel de l'érosion de marge sur contrats à prix fixe, par comparaison
        avec un indice de référence choisi par l'utilisateur (indice sectoriel, IPC,
        indice contractuel ou indice interne — saisi manuellement, pas d'accès API
        temps réel ici)."""
        c=cx()
        settings_row=c.execute('SELECT price_index_name FROM app_settings WHERE id=1').fetchone()
        index_name=(settings_row['price_index_name'] if settings_row and settings_row['price_index_name'] else 'INDICE').strip() or 'INDICE'
        if request.method=='POST':
            form=request.form.get('form_type')
            if form=='index_name':
                new_name=request.form.get('index_name','').strip() or 'INDICE'
                c.execute("INSERT INTO app_settings(id,price_index_name,updated_at) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET price_index_name=excluded.price_index_name,updated_at=excluded.updated_at",(new_name,now())); c.commit()
                flash(f"Indice de référence mis à jour : {new_name}.")
                c.close(); return redirect(url_for('margin_watch'))
            if form=='reading':
                d=request.form.get('reading_date','').strip(); v=request.form.get('value','').strip()
                try: v=float(v)
                except ValueError: v=None
                if d and v is not None:
                    c.execute('INSERT INTO price_index_readings(index_name,reading_date,value,created_at) VALUES(?,?,?,?)',(index_name,d,v,now())); c.commit()
                    flash(f'Relevé {index_name} enregistré.')
                else:
                    flash('Date et valeur requises pour un relevé.')
            elif form=='contract':
                name=request.form.get('project_name','').strip(); customer=request.form.get('customer','').strip()
                try: amount=float(request.form.get('amount','0'))
                except ValueError: amount=0
                signed=request.form.get('signed_date','').strip()
                try: mat_share=float(request.form.get('materials_share_pct','30'))
                except ValueError: mat_share=30
                if name and amount and signed:
                    c.execute('INSERT INTO fixed_price_contracts(project_name,customer,amount,signed_date,materials_share_pct,status,created_at) VALUES(?,?,?,?,?,?,?)',
                        (name,customer,amount,signed,mat_share,'ACTIVE',now())); c.commit()
                    flash('Contrat à prix fixe ajouté au suivi.')
                else:
                    flash('Nom du projet, montant et date de signature requis.')
            c.close(); return redirect(url_for('margin_watch'))

        readings=c.execute("SELECT * FROM price_index_readings WHERE index_name=? ORDER BY reading_date ASC",(index_name,)).fetchall()
        contracts=c.execute("SELECT * FROM fixed_price_contracts WHERE status='ACTIVE' ORDER BY signed_date DESC").fetchall()
        c.close()

        alerts=[]
        if readings:
            latest=readings[-1]
            for ct in contracts:
                # Indice le plus proche de la date de signature (le dernier relevé <= date signée,
                # sinon le premier relevé disponible si tous sont postérieurs).
                baseline=None
                for r in readings:
                    if r['reading_date']<=ct['signed_date']: baseline=r
                if baseline is None: baseline=readings[0]
                if baseline['value'] and latest['value'] and baseline['id']!=latest['id']:
                    change_pct=(latest['value']-baseline['value'])/baseline['value']*100
                    at_risk=ct['amount']*(ct['materials_share_pct']/100)*(change_pct/100)
                    alerts.append({'contract':ct,'baseline':baseline,'latest':latest,
                        'change_pct':round(change_pct,1),'at_risk':round(at_risk),
                        'is_erosion':change_pct>0})
        alerts.sort(key=lambda a:abs(a['at_risk']),reverse=True)

        return render_template('margin_watch.html',readings=readings,contracts=contracts,alerts=alerts,index_name=index_name)

    @app.route('/portfolio')
    @login_required
    def portfolio():
        """Vue consolidée de toutes les organisations de l'utilisateur — utile pour un
        cabinet comptable qui gère plusieurs clients depuis un seul compte."""
        orgs=user_organizations()
        rows=[]
        for org in orgs:
            try:
                tc=tenant_cx_direct(org['id'])
                recover=tc.execute("SELECT COALESCE(SUM(MAX(amount-paid_amount,0)),0) t FROM invoices WHERE LOWER(COALESCE(status,''))!='paid' AND days_overdue>0").fetchone()['t']
                save=tc.execute("SELECT COALESCE(SUM(value),0) t FROM opportunities WHERE type='SAVE' AND status='OPEN'").fetchone()['t']
                grow_n=tc.execute("SELECT COUNT(*) c FROM opportunities WHERE type='GROW' AND status='OPEN'").fetchone()['c']
                pending=tc.execute("SELECT COUNT(*) c FROM actions WHERE status='PENDING'").fetchone()['c']
                tc.close()
                rows.append({'org':org,'recover':recover,'save':save,'grow':grow_n,'pending':pending,'error':None})
            except Exception as e:
                rows.append({'org':org,'recover':0,'save':0,'grow':0,'pending':0,'error':str(e)})
        totals={'recover':sum(r['recover'] for r in rows),'save':sum(r['save'] for r in rows),
                'grow':sum(r['grow'] for r in rows),'pending':sum(r['pending'] for r in rows)}
        return render_template('portfolio.html',rows=rows,totals=totals)

    @app.route('/partners',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @require_area('grow')
    def partners():
        """Radar de partenaires : répertoire partagé opt-in entre organisations ProfitOS.
        Aucune donnée n'est visible pour les autres tant que l'organisation n'a pas activé
        explicitement le partage — action volontaire, jamais activée par défaut."""
        org=current_org(); ac=auth_cx()
        if request.method=='POST':
            action=request.form.get('action')
            if action=='opt_in':
                if current_role() not in ('OWNER','ADMIN'):
                    ac.close(); flash("Seuls le propriétaire ou un administrateur peuvent activer le partage."); return redirect(url_for('partners'))
                c=cx(); comp=c.execute('SELECT * FROM company WHERE id=1').fetchone(); c.close()
                if not comp or not comp['name']:
                    ac.close(); flash("Complète d'abord ton profil entreprise avant d'activer le partage."); return redirect(url_for('company'))
                contact=request.form.get('contact_email','').strip() or current_user()['email']
                ac.execute('''INSERT INTO partner_directory(organization_id,company_name,department,activities,contact_email,opted_in,updated_at)
                              VALUES(?,?,?,?,?,1,?)
                              ON CONFLICT(organization_id) DO UPDATE SET company_name=excluded.company_name,
                                department=excluded.department,activities=excluded.activities,
                                contact_email=excluded.contact_email,opted_in=1,updated_at=excluded.updated_at''',
                    (org['id'],comp['name'],comp['department'],comp['activities'],contact,now())); ac.commit()
                log_activity('PARTNER_DIRECTORY_OPT_IN','Profil rendu visible pour le radar de partenaires')
                flash('Ton profil est maintenant visible par les autres organisations ProfitOS dans le radar de partenaires.')
            elif action=='opt_out':
                ac.execute('UPDATE partner_directory SET opted_in=0,updated_at=? WHERE organization_id=?',(now(),org['id'])); ac.commit()
                log_activity('PARTNER_DIRECTORY_OPT_OUT','Profil retiré du radar de partenaires')
                flash('Ton profil a été retiré du radar de partenaires.')
            ac.close(); return redirect(url_for('partners'))

        my_entry=ac.execute('SELECT * FROM partner_directory WHERE organization_id=?',(org['id'],)).fetchone()
        matches=[]
        if my_entry and my_entry['opted_in']:
            my_terms=set(profile_terms({'activities':my_entry['activities']}))
            others=ac.execute('SELECT * FROM partner_directory WHERE opted_in=1 AND organization_id!=?',(org['id'],)).fetchall()
            for o in others:
                other_terms=set(profile_terms({'activities':o['activities']}))
                same_dept=bool(my_entry['department']) and my_entry['department']==o['department']
                complementary=bool(my_terms) and bool(other_terms) and not (my_terms & other_terms)
                if complementary:
                    matches.append({'org':o,'same_dept':same_dept})
            matches.sort(key=lambda m:not m['same_dept'])
        ac.close()
        return render_template('partners.html',my_entry=my_entry,matches=matches)

    @app.route('/pay-status/<token>')
    def public_invoice_status(token):
        """Portail client public — aucune authentification requise. N'expose QUE le strict
        nécessaire pour une facture précise (montant, statut, échéance), jamais le reste
        des données de l'organisation. Le token est non-devinable (20 octets aléatoires)."""
        mapping=resolve_public_invoice_token(token)
        if not mapping:
            abort(404)
        tc=tenant_cx_direct(mapping['organization_id'])
        inv=tc.execute('SELECT *,MAX(amount-paid_amount,0) outstanding FROM invoices WHERE id=?',(mapping['invoice_local_id'],)).fetchone()
        tc.close()
        if not inv:
            abort(404)
        ac=auth_cx(); org=ac.execute('SELECT name FROM organizations WHERE id=?',(mapping['organization_id'],)).fetchone(); ac.close()
        return render_template('pay_status.html',inv=inv,org_name=org['name'] if org else '')

    @app.route('/export-download/<token>')
    def export_download(token):
        """Téléchargement public authentifié par token — utilisé par le lien envoyé au
        comptable. Régénère l'export RECOVER à la demande (jamais de fichier stocké)."""
        ac=auth_cx()
        mapping=ac.execute('SELECT * FROM export_tokens WHERE token=?',(token,)).fetchone()
        ac.close()
        if not mapping:
            abort(404)
        tc=tenant_cx_direct(mapping['organization_id'])
        rows=tc.execute("SELECT *,MAX(amount-paid_amount,0) outstanding FROM invoices WHERE LOWER(COALESCE(status,''))!='paid' AND days_overdue>0 ORDER BY score DESC").fetchall()
        tc.close()
        data=[{'Client':r['customer'],'N° facture':r['invoice_number'],'Type':r['kind'],
               'Montant dû (€)':round(r['outstanding'],2),'Jours de retard':r['days_overdue'],
               "Date d'échéance":r['due_date'],'Statut':r['status'],'Score':r['score']} for r in rows]
        return export_response(data,'profitos-recover')

    @app.route('/profit-audit',methods=['GET','POST'])
    def public_profit_audit():
        """Calculateur public sans compte — aimant à leads. Estimation indicative
        basée sur des heuristiques générales, pas sur de vraies données client
        (le visiteur n'a encore rien uploadé)."""
        result=None
        if request.method=='POST':
            try:
                revenue=float(request.form.get('revenue','0').replace(' ','').replace(',','.'))
                overdue_invoices=int(request.form.get('overdue_invoices','0'))
            except ValueError:
                revenue=0; overdue_invoices=0
            revenue=max(0,min(revenue,50_000_000))
            overdue_invoices=max(0,min(overdue_invoices,200))
            # Heuristique indicative : ~1,5% du CA annuel est généralement en créances
            # en retard, ajusté par le nombre de factures en retard déclaré.
            base_recoverable=revenue*0.015
            adjustment=1+min(overdue_invoices,20)*0.05
            recoverable_low=round(base_recoverable*adjustment*0.6,-2)
            recoverable_high=round(base_recoverable*adjustment*1.4,-2)
            savings_estimate=round(revenue*0.004,-2)
            result={'recoverable_low':recoverable_low,'recoverable_high':recoverable_high,'savings_estimate':savings_estimate}
        return render_template('profit_audit_public.html',result=result)

    @app.route('/demo',methods=['GET','POST'])
    def request_demo():
        """Demande de démo publique — aucune authentification requise."""
        if request.method=='POST':
            full_name=request.form.get('full_name','').strip()
            company=request.form.get('company','').strip()
            email=request.form.get('email','').strip()
            phone=request.form.get('phone','').strip()
            message=request.form.get('message','').strip()
            if not (full_name and company and email):
                flash('Nom, entreprise et email sont requis.')
                return redirect(request.url)
            ac=auth_cx()
            ac.execute('INSERT INTO demo_requests(full_name,company,email,phone,message,created_at) VALUES(?,?,?,?,?,?)',
                (full_name,company,email,phone,message,now())); ac.commit(); ac.close()
            admin_email=os.environ.get('DEMO_NOTIFY_EMAIL','')
            if admin_email:
                html=render_template('email_transactional.html',title='Nouvelle demande de démo',
                    intro=f"{full_name} ({company}, {email}{', '+phone if phone else ''}) a demandé une démo ProfitOS.{' Message : '+message if message else ''}",
                    cta_label=f'Répondre à {full_name}',cta_url=f'mailto:{email}',footer='')
                send_email(admin_email,f'Demande de démo — {company}',html)
            return render_template('demo_thanks.html')
        return render_template('demo_request.html')

    @app.route('/pricing')
    def pricing():
        """Page tarifs publique — aucune authentification requise."""
        return render_template('pricing.html',plans=STRIPE_PLANS,plan_limits=PLAN_LIMITS)

    @app.route('/customer-tags/set',methods=['POST'])
    @login_required
    @requires_active_plan
    @require_area('recover')
    def customer_tag_set():
        """Étiquette un client (VIP/RISQUE/FIABLE) — affichée sur RECOVER pour aider
        à prioriser visuellement, sans impacter les calculs de score existants."""
        customer=request.form.get('customer','').strip()
        tag=request.form.get('tag','').strip().upper()
        note=request.form.get('note','').strip()
        if not customer:
            flash('Client requis.'); return redirect(request.referrer or url_for('recover'))
        if tag not in ('VIP','RISQUE','FIABLE','',None):
            tag=''
        c=cx()
        if tag:
            c.execute('''INSERT INTO customer_tags(customer_name_norm,customer_name_display,tag,note,updated_at)
                         VALUES(?,?,?,?,?)
                         ON CONFLICT(customer_name_norm) DO UPDATE SET
                           customer_name_display=excluded.customer_name_display,tag=excluded.tag,
                           note=excluded.note,updated_at=excluded.updated_at''',
                (norm(customer),customer,tag,note,now()))
        else:
            c.execute('DELETE FROM customer_tags WHERE customer_name_norm=?',(norm(customer),))
        c.commit(); c.close()
        flash(f"Étiquette mise à jour pour {customer}." if tag else f"Étiquette retirée pour {customer}.")
        return redirect(request.referrer or url_for('recover'))

    @app.route('/calendar')
    @login_required
    @requires_active_plan
    @require_area('recover')
    def calendar_view():
        """Vue unifiée des échéances : factures en retard, retenues contractuelles
        libérables, deadlines GROW — tout au même endroit, triées par date."""
        c=cx()
        events=[]
        for r in c.execute("SELECT invoice_number,customer,due_date,MAX(amount-paid_amount,0) outstanding FROM invoices WHERE LOWER(COALESCE(status,''))!='paid' AND kind!='RETENTION' AND due_date IS NOT NULL").fetchall():
            events.append({'date':r['due_date'],'type':'RECOVER','label':f"Facture #{r['invoice_number']} — {r['customer']}",'value':r['outstanding']})
        for r in c.execute("SELECT invoice_number,customer,retention_release_date,MAX(amount-paid_amount,0) outstanding FROM invoices WHERE kind='RETENTION' AND LOWER(COALESCE(status,''))!='paid' AND retention_release_date IS NOT NULL").fetchall():
            events.append({'date':r['retention_release_date'],'type':'RETENTION','label':f"Retenue libérable — {r['customer']} (#{r['invoice_number']})",'value':r['outstanding']})
        if can_access('grow'):
            for r in c.execute("SELECT title,buyer,deadline FROM opportunities WHERE type='GROW' AND status='OPEN' AND deadline IS NOT NULL").fetchall():
                events.append({'date':r['deadline'],'type':'GROW','label':f"{r['title']} — {r['buyer'] or ''}",'value':0})
        c.close()
        events.sort(key=lambda e:e['date'] or '')
        today_iso=date.today().isoformat()
        past=[e for e in events if e['date']<today_iso]
        upcoming=[e for e in events if e['date']>=today_iso]
        return render_template('calendar.html',upcoming=upcoming,past_count=len(past))


    def _recover_filtered_rows():
        """Applique les filtres/recherche de la query string RECOVER (filter, q, min_amount,
        max_amount, date_from, date_to). Partagé entre l'écran RECOVER et son export."""
        c=cx()
        active=c.execute("SELECT *,MAX(amount-paid_amount,0) outstanding FROM invoices WHERE LOWER(COALESCE(status,''))!='paid' AND days_overdue>0 ORDER BY score DESC").fetchall()
        total=sum(x['outstanding'] for x in active)
        filt=request.args.get('filter','all')
        if filt=='overdue':
            rows=[r for r in active if r['kind']!='RETENTION']
        elif filt=='retention':
            rows=c.execute("SELECT *,MAX(amount-paid_amount,0) outstanding FROM invoices WHERE kind='RETENTION' AND LOWER(COALESCE(status,''))!='paid' ORDER BY COALESCE(retention_release_date,due_date) ASC").fetchall()
        else:
            rows=active
        c.close()
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
        return rows,total,filt,q,min_amount,max_amount,date_from,date_to

    @app.route('/recover')
    @login_required
    @requires_active_plan
    @require_area('recover')
    def recover():
        rows,total,filt,q,min_amount,max_amount,date_from,date_to=_recover_filtered_rows()
        c=cx(); tags=c.execute('SELECT * FROM customer_tags').fetchall(); c.close()
        tags_map={t['customer_name_norm']:t['tag'] for t in tags}
        for r in rows: r['customer_tag']=tags_map.get(norm(r['customer']))
        return render_template('recover.html',rows=rows,total=total,filt=filt,today=date.today().isoformat(),
            q=q,min_amount=min_amount,max_amount=max_amount,date_from=date_from,date_to=date_to)

    @app.route('/recover/export')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('recover')
    def recover_export():
        org=current_org()
        quota=quota_state('reports_per_month',organization_id=org['id'],plan=org['plan'])
        if not quota['allowed']:
            flash(f"Quota mensuel d'exports atteint pour la formule {org['plan']} ({quota['used']}/{quota['limit']}). Passez à une formule supérieure.")
            return redirect(url_for('recover'))
        rows,total,filt,q,min_amount,max_amount,date_from,date_to=_recover_filtered_rows()
        data=[{'Client':r['customer'],'N° facture':r['invoice_number'],'Type':r['kind'],
               'Montant dû (€)':round(r['outstanding'],2),'Jours de retard':r['days_overdue'],
               "Date d'échéance":r['due_date'],'Statut':r['status'],'Score':r['score']} for r in rows]
        record_usage('reports_per_month',organization_id=org['id'])
        return export_response(data,'profitos-recover')

    def _filter_rows(rows,q,min_value,max_value,fields,numeric_field='value'):
        """Filtre générique texte + min/max sur un champ numérique, réutilisé par SAVE et GROW."""
        if q:
            qn=norm(q); rows=[r for r in rows if any(qn in norm(r[f] or '') for f in fields)]
        if min_value:
            try: mn=float(min_value); rows=[r for r in rows if (r[numeric_field] or 0)>=mn]
            except (ValueError,KeyError): pass
        if max_value:
            try: mx=float(max_value); rows=[r for r in rows if (r[numeric_field] or 0)<=mx]
            except (ValueError,KeyError): pass
        return rows

    @app.route('/save')
    @login_required
    @requires_active_plan
    @require_area('save')
    def save():
        c=cx(); rows=c.execute("SELECT * FROM opportunities WHERE type='SAVE' AND status='OPEN' ORDER BY score DESC").fetchall(); c.close()
        total=sum(x['value'] for x in rows)
        q=request.args.get('q','').strip(); min_value=request.args.get('min_value','').strip(); max_value=request.args.get('max_value','').strip()
        rows=_filter_rows(rows,q,min_value,max_value,['title','details'])
        return render_template('save.html',rows=rows,total=total,q=q,min_value=min_value,max_value=max_value)

    @app.route('/save/export')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('save')
    def save_export():
        org=current_org()
        quota=quota_state('reports_per_month',organization_id=org['id'],plan=org['plan'])
        if not quota['allowed']:
            flash(f"Quota mensuel d'exports atteint pour la formule {org['plan']} ({quota['used']}/{quota['limit']}). Passez à une formule supérieure.")
            return redirect(url_for('save'))
        c=cx(); rows=c.execute("SELECT * FROM opportunities WHERE type='SAVE' AND status='OPEN' ORDER BY score DESC").fetchall(); c.close()
        data=[{'Titre':r['title'],'Valeur estimée (€/an)':round(r['value'],2),'Score':r['score'],
               'Détails':r['details'],'Source':r['source']} for r in rows]
        record_usage('reports_per_month',organization_id=org['id'])
        return export_response(data,'profitos-save')

    @app.route('/grow')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('grow')
    def grow():
        p=profile()
        if not p:return render_template('grow.html',rows=[],needs_profile=True,last=None,jlist=jlist,fmt_deadline=fmt_deadline,days_left=days_left,q='',min_value='',max_value='')
        c=cx(); rows=c.execute("SELECT * FROM opportunities WHERE type='GROW' AND status='OPEN' ORDER BY score DESC").fetchall(); last=c.execute("SELECT * FROM audit_runs WHERE run_type='GROW' ORDER BY id DESC LIMIT 1").fetchone(); c.close()
        q=request.args.get('q','').strip(); min_value=request.args.get('min_value','').strip(); max_value=request.args.get('max_value','').strip()
        rows=_filter_rows(rows,q,min_value,max_value,['title','buyer'],numeric_field='score')
        return render_template('grow.html',rows=rows,needs_profile=False,last=last,jlist=jlist,fmt_deadline=fmt_deadline,days_left=days_left,q=q,min_value=min_value,max_value=max_value)

    @app.route('/grow/export')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('grow')
    def grow_export():
        org=current_org()
        quota=quota_state('reports_per_month',organization_id=org['id'],plan=org['plan'])
        if not quota['allowed']:
            flash(f"Quota mensuel d'exports atteint pour la formule {org['plan']} ({quota['used']}/{quota['limit']}). Passez à une formule supérieure.")
            return redirect(url_for('grow'))
        c=cx(); rows=c.execute("SELECT * FROM opportunities WHERE type='GROW' AND status='OPEN' ORDER BY score DESC").fetchall(); c.close()
        data=[{'Titre':r['title'],'Acheteur':r['buyer'],'Score':r['score'],
               'Départements':r['departments'],'Échéance':r['deadline'],'Source':r['source']} for r in rows]
        record_usage('reports_per_month',organization_id=org['id'])
        return export_response(data,'profitos-grow')

    @app.route('/grow/refresh',methods=['POST'])
    @login_required
    @requires_paid_plan
    @require_area('grow')
    @requires_feature('advanced_features')
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
        if kind=='GROW' and not current_plan_is_paid():
            return _deny_paid_feature('grow')
        if not can_access(KIND_TO_AREA.get(kind,'')):
            flash("Votre rôle ne donne pas accès à cette section."); return redirect(url_for('home'))
        c=cx()
        if kind=='RECOVER':
            r=c.execute('SELECT *,MAX(amount-paid_amount,0) outstanding FROM invoices WHERE id=?',(item_id,)).fetchone()
            if not r:abort(404)
            if r['kind']=='RETENTION':
                release=r['retention_release_date']
                if release and release<=date.today().isoformat():
                    reasons=[f"Retenue contractuelle libérable depuis le {release}",'contactez le client pour en demander la levée']
                elif release:
                    reasons=[f"Retenue contractuelle libérable le {release}",'pas encore actionnable — visible pour anticipation']
                else:
                    reasons=['Retenue contractuelle sans date de libération connue — à clarifier avec le contrat']
                o=dict(r); o.update(kind='RECOVER',title=f"Retenue contractuelle — Facture #{r['invoice_number']}",value=r['outstanding'],reasons=reasons,warnings=['la retenue contractuelle suit un régime différent d\'une facture standard'])
                # Simulateur de caution : remplacer la retenue en numéraire par une caution
                # bancaire libère la trésorerie immédiatement, contre un coût annuel en %.
                try: bond_rate=float(request.args.get('bond_rate','1.0'))
                except ValueError: bond_rate=1.0
                bond_rate=max(0.1,min(bond_rate,10.0))
                days_to_release=None
                if release:
                    try: days_to_release=max(0,(datetime.strptime(release,'%Y-%m-%d').date()-date.today()).days)
                    except ValueError: pass
                bond_cost=None
                if days_to_release is not None:
                    bond_cost=r['outstanding']*(bond_rate/100)*(days_to_release/365)
                o['bond_rate']=bond_rate; o['bond_cost']=bond_cost; o['days_to_release']=days_to_release
            else:
                o=dict(r); o.update(kind='RECOVER',title=f"Facture #{r['invoice_number']}",value=r['outstanding'],reasons=[f"{r['days_overdue']} jours de retard",'montant calculé depuis les données importées'],warnings=[])
            try:
                o['buyer_risk']=buyer_risk_lookup(r['customer'],session['org_id'])
            except Exception:
                o['buyer_risk']=None
            tag_row=c.execute('SELECT tag,note FROM customer_tags WHERE customer_name_norm=?',(norm(r['customer']),)).fetchone()
            o['customer_tag']=tag_row['tag'] if tag_row else None
            o['customer_tag_note']=tag_row['note'] if tag_row else ''
        else:
            r=c.execute('SELECT * FROM opportunities WHERE id=? AND type=?',(item_id,kind)).fetchone()
            if not r:abort(404)
            o=dict(r); o.update(kind=kind,reasons=json.loads(r['reasons'] or '[]'),warnings=json.loads(r['warnings'] or '[]'),departments=jlist(r['departments']),deadline_human=fmt_deadline(r['deadline']),days_remaining=days_left(r['deadline']))
        acts=c.execute('SELECT * FROM actions WHERE opportunity_id=? AND kind=? ORDER BY id DESC',(item_id,kind)).fetchall()
        org=current_org()
        can_use_advanced=bool(org and feature_enabled(org['plan'],'advanced_features'))
        dce=c.execute('SELECT * FROM dce_documents WHERE opportunity_id=? ORDER BY id DESC',(item_id,)).fetchall() if kind=='GROW' and can_use_advanced else []
        history=c.execute('SELECT * FROM status_history WHERE kind=? AND entity_id=? ORDER BY id DESC',(kind,item_id)).fetchall()
        c.close(); dce_items=[]
        for d in dce:
            x=dict(d); x['analysis']=json.loads(d['analysis_json'] or '{}'); dce_items.append(x)
        return render_template('detail.html',o=o,actions=acts,dce_items=dce_items,history=history)

    @app.route('/opportunity/<kind>/<int:item_id>/status',methods=['POST'])
    @login_required
    def opportunity_status(kind,item_id):
        kind=kind.upper()
        if kind=='GROW' and not current_plan_is_paid():
            return _deny_paid_feature('grow_status')
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

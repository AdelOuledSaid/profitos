from profitos.runtime import *



def register(app):
    @app.route('/upload/invoices',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @require_area('uploads')
    def upload_invoices():
        if request.method=='POST':
            f=request.files.get('file')
            if not f:return redirect(request.url)
            try:
                path, original_name=save_upload(f,'imports',ALLOWED_INVOICE_EXTENSIONS)
                df=pd.read_excel(path) if path.suffix.lower() in ('.xlsx','.xls') else pd.read_csv(path)
            except Exception as e:
                flash(f'Import refusé : {e}'); return redirect(request.url)
            finally:
                if 'path' in locals(): cleanup_upload(path)
            aliases={'num':['invoice_number','invoice','number','numero','numéro','n° facture','facture'],'customer':['customer','client','customer_name','nom client'],'amount':['amount','montant','montant ttc','total'],'paid':['paid_amount','montant payé','montant paye'],'issue':['issue_date','date facture','date'],'due':['due_date','échéance','echeance',"date d'échéance"],'status':['status','statut','etat','état'],'itype':['type','nature','kind'],'release':['retention_release_date','date liberation','date de liberation','date de levée','date de levee','date liberation retenue'],'email':['customer_email','email','email client','courriel','mail client']}; m=map_cols(df,aliases); missing=[k for k in ('num','customer','amount','due') if k not in m]
            if missing:flash('Colonnes manquantes : '+', '.join(missing)); return redirect(request.url)
            RETENTION_KEYWORDS=('retenue','retention','garantie')
            c=cx(); c.execute('DELETE FROM invoices'); today=date.today(); signals=0; retentions=0
            for _,r in df.iterrows():
                try:amount=float(r[m['amount']])
                except:continue
                paid=money(r[m['paid']]) if 'paid' in m else 0; issue=parse_date(r[m['issue']]) if 'issue' in m else None; status=str(r[m['status']]) if 'status' in m else ('paid' if paid>=amount else 'unpaid')
                itype_raw=norm(str(r[m['itype']])) if 'itype' in m and str(r[m['itype']]).strip().lower()!='nan' else ''
                is_retention=any(k in itype_raw for k in RETENTION_KEYWORDS)
                release=parse_date(r[m['release']]) if 'release' in m else None
                due=parse_date(r[m['due']])
                if is_retention:
                    kind='RETENTION'; ref_date=release or due
                    days=max(0,(today-ref_date).days) if ref_date and ref_date<=today else 0
                    retentions+=1
                else:
                    kind='STANDARD'; ref_date=due
                    days=max(0,(today-due).days) if due else 0
                score=invoice_score(amount,days,paid) if norm(status)!='paid' and days>0 else 0; signals+=score>0
                email_val=str(r[m['email']]).strip() if 'email' in m and str(r[m['email']]).strip().lower()!='nan' else None
                c.execute('INSERT INTO invoices(invoice_number,customer,amount,paid_amount,issue_date,due_date,status,days_overdue,score,created_at,kind,retention_release_date,customer_email) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (str(r[m['num']]),str(r[m['customer']]),amount,paid,issue.isoformat() if issue else None,due.isoformat() if due else None,status,days,score,now(),kind,release.isoformat() if release else None,email_val))
            c.commit()
            # Snapshot DSO : moyenne des jours de retard pondérée, un point par jour (upsert si déjà uploadé aujourd'hui).
            snap=c.execute("SELECT AVG(days_overdue) avg_d,COALESCE(SUM(MAX(amount-paid_amount,0)),0) total,COUNT(*) n FROM invoices WHERE LOWER(COALESCE(status,''))!='paid' AND days_overdue>0").fetchone()
            today_iso=today.isoformat()
            c.execute('''INSERT INTO dso_snapshots(snapshot_date,avg_days_overdue,total_outstanding,invoice_count,created_at)
                         VALUES(?,?,?,?,?)
                         ON CONFLICT(snapshot_date) DO UPDATE SET
                           avg_days_overdue=excluded.avg_days_overdue,
                           total_outstanding=excluded.total_outstanding,
                           invoice_count=excluded.invoice_count,
                           created_at=excluded.created_at''',
                (today_iso,snap['avg_d'] or 0,snap['total'] or 0,snap['n'] or 0,now()))
            c.commit()
            # Notification Slack/Teams si des opportunités urgentes (score >= 90) viennent d'être détectées.
            urgent=c.execute("SELECT COUNT(*) n,COALESCE(SUM(MAX(amount-paid_amount,0)),0) t FROM invoices WHERE LOWER(COALESCE(status,''))!='paid' AND days_overdue>0 AND score>=90").fetchone()
            c.close()
            if urgent['n']:
                notify_org(f"🔴 ProfitOS · {urgent['n']} facture(s) en retard critique détectée(s) — {urgent['t']:,.0f} € à risque élevé.")
            msg=f'Factures analysées · {signals} signaux RECOVER.'
            if retentions: msg+=f' Dont {retentions} retenue(s) de garantie détectée(s).'
            flash(msg); return redirect(url_for('recover'))
        return render_template('upload.html',title='Importer les factures',kind='invoices')

    RENEWABLE_CATEGORIES={
     'assurance':['assurance','insurance','décennale','decennale','responsabilite civile','responsabilité civile'],
     'energie':['energie','énergie','electricite','électricité','gaz','edf','engie','total energies'],
     'telecom':['telecom','téléphonie','telephonie','internet','fibre','abonnement mobile'],
     'logiciel':['logiciel','saas','abonnement logiciel','licence'],
    }

    def detect_duplicates(clean):
        """Doublons potentiels : même fournisseur, montant et date."""
        out=[]
        counter=Counter((norm(v),round(a,2),d.isoformat() if d else '') for v,a,d,cat in clean)
        for (v,a,d),n in counter.items():
            if n>1:
                out.append(dict(title=f'Doublon potentiel — {v.title()}',value=a*(n-1),score=90,
                    details=f'{n} dépenses identiques détectées le {d}.',
                    reasons=['même fournisseur, montant et date'],warnings=['vérification humaine requise']))
        return out

    def detect_price_increases(clean):
        """Hausse fournisseur : dépense mensuelle en hausse de plus de 20% d'un mois sur l'autre."""
        out=[]
        monthly=defaultdict(lambda:defaultdict(float))
        for v,a,d,cat in clean:
            if d:monthly[v][d.strftime('%Y-%m')]+=a
        for v,vals in monthly.items():
            ms=sorted(vals)
            if len(ms)>=2 and vals[ms[-2]]>0 and vals[ms[-1]]>vals[ms[-2]]*1.2:
                prev,curr=vals[ms[-2]],vals[ms[-1]]
                out.append(dict(title=f'Hausse fournisseur — {v}',value=(curr-prev)*12,score=75,
                    details=f'{prev:,.0f} € → {curr:,.0f} € entre les deux derniers mois.',
                    reasons=['hausse mensuelle supérieure à 20 %'],warnings=['annualisation indicative']))
        return out

    def detect_stale_contracts(clean):
        """Contrats potentiellement à renouveler : assurance/énergie/télécom/logiciel sans
        dépense détectée depuis plus de 11 mois, alors qu'une dépense existait avant.
        Heuristique : un contrat récurrent qui "disparaît" des dépenses est souvent le signe
        d'un renouvellement en cours de négociation (le devis n'a pas encore été signé) ou
        d'un contrat expiré passé inaperçu."""
        out=[]; today=date.today()
        by_vendor_cat=defaultdict(list)
        for v,a,d,cat in clean:
            fam=next((fam for fam,kws in RENEWABLE_CATEGORIES.items() if any(k in norm(cat) or k in norm(v) for k in kws)),None)
            if fam and d: by_vendor_cat[(fam,norm(v))].append((d,a))
        for (fam,v),rows in by_vendor_cat.items():
            rows.sort(); last_date,last_amount=rows[-1]; gap_days=(today-last_date).days
            if gap_days>=330 and len(rows)>=1:
                out.append(dict(title=f'Contrat à vérifier — {v.title()} ({fam})',value=round(last_amount,0),score=70,
                    details=f'Dernière dépense {fam} détectée il y a {gap_days} jours ({last_date.isoformat()}).',
                    reasons=[f'catégorie {fam} récurrente sans dépense récente'],
                    warnings=['obtenir 3 devis avant renouvellement automatique']))
        return out

    def run_save_engine(clean):
        """clean = liste de tuples (vendor, amount, date, category). Retourne les opportunités SAVE."""
        return detect_duplicates(clean)+detect_price_increases(clean)+detect_stale_contracts(clean)

    @app.route('/upload/expenses',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @require_area('uploads')
    def upload_expenses():
        if request.method=='POST':
            f=request.files.get('file')
            if not f:return redirect(request.url)
            try:
                path, original_name=save_upload(f,'imports',ALLOWED_INVOICE_EXTENSIONS)
                df=pd.read_excel(path) if path.suffix.lower() in ('.xlsx','.xls') else pd.read_csv(path)
            except Exception as e:
                flash(f'Import refusé : {e}'); return redirect(request.url)
            finally:
                if 'path' in locals(): cleanup_upload(path)
            aliases={'vendor':['vendor','supplier','fournisseur'],'desc':['description','libelle','libellé'],'amount':['amount','montant','total'],'date':['date','expense_date','date dépense','date depense'],'cat':['category','categorie','catégorie']}; m=map_cols(df,aliases); missing=[k for k in ('vendor','amount','date') if k not in m]
            if missing:flash('Colonnes manquantes : '+', '.join(missing)); return redirect(request.url)
            c=cx(); c.execute('DELETE FROM expenses'); c.execute("DELETE FROM opportunities WHERE type='SAVE'"); clean=[]
            for _,r in df.iterrows():
                try:a=float(r[m['amount']])
                except:continue
                d=parse_date(r[m['date']]); v=str(r[m['vendor']]).strip(); desc=str(r[m['desc']]) if 'desc' in m else ''; cat=str(r[m['cat']]) if 'cat' in m else ''; c.execute('INSERT INTO expenses(vendor,description,amount,expense_date,category) VALUES(?,?,?,?,?)',(v,desc,a,d.isoformat() if d else None,cat)); clean.append((v,a,d,cat))
            for opp in run_save_engine(clean):
                c.execute("INSERT INTO opportunities(type,title,value,score,details,source,reasons,warnings,status,created_at) VALUES('SAVE',?,?,?,?,?,?,?,'OPEN',?)",
                    (opp['title'],opp['value'],opp['score'],opp['details'],'Expense Engine',
                     json.dumps(opp['reasons'],ensure_ascii=False),json.dumps(opp['warnings'],ensure_ascii=False),now()))
            c.commit(); c.close(); flash('Dépenses analysées.'); return redirect(url_for('save'))
        return render_template('upload.html',title='Importer les dépenses',kind='expenses')

    if __name__=='__main__':
        print('='*60)
        if dbmod.USE_POSTGRES:
            safe_url=re.sub(r'://([^:]+):[^@]+@', r'://\1:***@', dbmod.DATABASE_URL)
            print(f'[ProfitOS] Base de données : PostgreSQL -> {safe_url}')
        else:
            print(f'[ProfitOS] Base de données : SQLite (local) -> {AUTH_DB}')
            print('[ProfitOS] Pour utiliser Postgres, vérifie que DATABASE_URL est bien définie (fichier .env).')
        print('='*60)
        init_auth_db(); app.run(host='127.0.0.1',port=5050,debug=True)

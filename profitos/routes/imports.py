from profitos.runtime import *
from profitos.plan_usage import quota_state, record_usage
from profitos.feature_access import requires_paid_plan

# Formats de données acceptés par les imports financiers.
# Déclarés localement pour éviter qu'une ancienne constante runtime bloque les PDF.
FINANCIAL_IMPORT_EXTENSIONS={'.csv','.xlsx','.xls','.pdf'}



def _pdf_money_value(raw):
    """Convertit un montant FR/EN extrait d'un PDF en float, sans inventer de valeur."""
    if raw is None:
        return None
    v=str(raw).replace('\u00a0',' ').replace('€','').strip()
    v=re.sub(r'[^0-9,\.\- ]','',v).replace(' ','')
    if not v:
        return None
    if ',' in v and '.' in v:
        # 1.234,56 ou 1,234.56
        if v.rfind(',') > v.rfind('.'):
            v=v.replace('.','').replace(',','.')
        else:
            v=v.replace(',','')
    elif ',' in v:
        v=v.replace(',','.')
    try:
        return float(v)
    except Exception:
        return None


def _pdf_first(patterns, text, flags=re.I|re.M):
    for pat in patterns:
        m=re.search(pat,text,flags)
        if m:
            return m.group(1).strip()
    return None


def _pdf_text(path, max_pages=20):
    """Extrait le texte d'un PDF natif. Pas d'OCR : un scan image est refusé."""
    reader=PdfReader(str(path))
    pages=[]
    for page in reader.pages[:max_pages]:
        try:
            pages.append(page.extract_text() or '')
        except Exception:
            pages.append('')
    text='\n'.join(pages).strip()
    if len(text)<20:
        raise ValueError("PDF sans texte exploitable (probablement scanné). Utilisez un PDF texte ou le CSV/XLSX pour cette version.")
    return text


def _classify_expense_document(text, vendor=''):
    """Classe un document de dépense à partir de marqueurs explicites, sans IA externe."""
    hay=norm((vendor or '')+' '+text)
    rules=[
        ('Cotisations sociales - URSSAF', [r'\burssaf\b', r'cotisations? sociales?', r'declaration sociale nominative', r'\bdsn\b']),
        ('TVA', [r'\btva\b', r'taxe sur la valeur ajoutee', r'ca3', r'ca12']),
        ('Impôts et taxes', [r'dgfip', r'impots?\.gouv', r'impot sur les societes', r'\bis\b', r'\bcfe\b', r'cotisation fonciere', r'taxe sur les salaires']),
        ('Salaires et paie', [r'bulletin de paie', r'fiche de paie', r'salaire net', r'net a payer avant impot']),
        ('Assurance', [r'assurance', r'assureur', r'prime d.assurance']),
        ('Énergie', [r'electricite', r'energie', r'\bedf\b', r'engie', r'gaz naturel']),
        ('Télécom / Internet', [r'telecom', r'internet', r'orange', r'sfr', r'bouygues telecom', r'free pro']),
        ('Loyer / Immobilier', [r'loyer', r'avis d.echeance', r'bail']),
        ('Banque / Frais financiers', [r'frais bancaires?', r'commission bancaire', r'agios']),
        ('Logiciels / Abonnements', [r'abonnement', r'licence logiciel', r'saas']),
    ]
    for category,patterns in rules:
        if any(re.search(pat,hay,re.I) for pat in patterns):
            return category
    return 'Facture fournisseur'


def _extract_expense_pdf(path):
    """Extrait une dépense depuis un PDF texte : fournisseur, URSSAF, TVA, impôts, paie, etc."""
    text=_pdf_text(path, max_pages=20)

    amount_raw=_pdf_first([
        r'(?:total\s*ttc|net\s*(?:à|a)\s*payer|montant\s*(?:à|a)\s*payer|montant\s*du|amount\s*due|total\s*due)\s*[:\-]?\s*([0-9][0-9\s.,]*\s*€?)',
        r'(?:solde\s*(?:à|a)\s*payer|total)\s*[:\-]?\s*([0-9][0-9\s.,]*\s*€)',
    ], text)
    amount=_pdf_money_value(amount_raw)

    date_raw=_pdf_first([
        r'(?:date\s+limite\s+de\s+paiement|date\s+d[’\']?échéance|échéance|echeance|date\s+de\s+prélèvement|date\s+de\s+prelevement)\s*[:\-]?\s*([0-3]?\d[\/\-.][01]?\d[\/\-.](?:20)?\d{2})',
        r'(?:date\s+d[’\']?émission|date\s+facture|invoice\s+date|date)\s*[:\-]?\s*([0-3]?\d[\/\-.][01]?\d[\/\-.](?:20)?\d{2})',
        r'(?:date\s+d[’\']?émission|date\s+facture|invoice\s+date|date)\s*[:\-]?\s*((?:20)?\d{2}[\/\-.][01]?\d[\/\-.][0-3]?\d)',
    ], text)
    expense_date=parse_date(date_raw) if date_raw else None

    # Organismes publics / sociaux : on privilégie le nom officiel plutôt qu'une ligne d'en-tête arbitraire.
    ntext=norm(text)
    if re.search(r'\burssaf\b',ntext):
        vendor='URSSAF'
    elif re.search(r'dgfip|impots?\.gouv|direction generale des finances publiques',ntext):
        vendor='DGFiP / Impôts'
    else:
        vendor=_pdf_first([
            r'(?:fournisseur|supplier|vendor|créancier|creancier|organisme)\s*[:\-]\s*([^\n]{2,120})',
            r'(?:émis\s+par|emis\s+par|issued\s+by)\s*[:\-]\s*([^\n]{2,120})',
        ], text)

    if not vendor:
        for line in [x.strip() for x in text.splitlines()[:20] if x.strip()]:
            if len(line)<2 or len(line)>120: continue
            if re.search(r'^(facture|invoice|reçu|recu|receipt|date|n[°o]|total|client|bill\s*to|avis|déclaration|declaration)\b', line, re.I): continue
            if re.fullmatch(r'[0-9\s.,€+\-/]+', line): continue
            vendor=line; break

    category=_classify_expense_document(text,vendor)
    num=_pdf_first([
        r'(?:facture|invoice)\s*(?:n(?:°|o)?|num(?:e|é)ro|#)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9._\-/]{2,})',
        r'(?:référence|reference)\s*[:\-]\s*([A-Z0-9][A-Z0-9._\-/]{2,})',
    ], text)
    if category=='Cotisations sociales - URSSAF': desc='Cotisations sociales URSSAF'
    elif category=='TVA': desc='TVA'
    elif category=='Impôts et taxes': desc='Impôts et taxes'
    elif num: desc=f'Facture {num}'
    else: desc='Dépense importée depuis PDF'

    missing=[]
    if not vendor: missing.append('fournisseur / organisme')
    if amount is None or amount<=0: missing.append('montant à payer')
    if not expense_date: missing.append('date / échéance')
    if missing:
        raise ValueError('Champs non détectés dans le PDF : '+', '.join(missing)+'. Vérifiez que ces informations sont écrites explicitement dans le document.')

    return {'vendor':vendor,'description':desc,'amount':amount,'date':expense_date.isoformat(),'category':category}


def _extract_bank_statement_pdf(path):
    """Extrait des lignes de relevé depuis un PDF texte.

    Le parseur reste volontairement conservateur : une ligne doit contenir une date et
    un montant. Les PDF scannés ne sont pas pris en charge sans OCR.
    """
    text=_pdf_text(path, max_pages=40)
    rows=[]
    date_pat=re.compile(r'(?<!\d)([0-3]?\d[\/\-.][01]?\d[\/\-.](?:20)?\d{2}|(?:20)\d{2}[\/\-.][01]?\d[\/\-.][0-3]?\d)(?!\d)')
    amount_pat=re.compile(r'(?<!\d)([-+]?\s*\d{1,3}(?:[ .]\d{3})*(?:,\d{2}|\.\d{2})|[-+]?\s*\d+(?:,\d{2}|\.\d{2}))(?:\s*€)?(?!\d)')

    for raw_line in text.splitlines():
        line=' '.join(raw_line.split())
        if len(line)<6:
            continue
        dm=date_pat.search(line)
        if not dm:
            continue
        amounts=list(amount_pat.finditer(line))
        if not amounts:
            continue
        # La dernière valeur monétaire de la ligne correspond le plus souvent à la colonne montant/crédit.
        am=amounts[-1]
        amount=_pdf_money_value(am.group(1))
        if amount is None:
            continue
        d=parse_date(dm.group(1))
        if not d:
            continue
        desc=(line[dm.end():am.start()].strip(' -–—;:') or 'Opération bancaire')[:240]
        rows.append({'date':d.isoformat(),'description':desc,'amount':amount})

    # Élimine les doublons exacts éventuellement créés par les en-têtes/pieds répétés.
    unique=[]; seen=set()
    for row in rows:
        key=(row['date'],row['description'],round(float(row['amount']),2))
        if key in seen:
            continue
        seen.add(key); unique.append(row)
    if not unique:
        raise ValueError("Aucune ligne bancaire exploitable détectée dans ce PDF. Vérifiez qu'il contient du texte et des lignes avec date + montant.")
    return pd.DataFrame(unique)


def _extract_invoice_pdf(path):
    """Extraction prudente d'une facture PDF texte.

    Cette première version ne fait pas d'OCR. Elle accepte les PDF dont le texte est
    réellement extractible et refuse les documents ambigus au lieu d'inventer des champs.
    """
    text=_pdf_text(path, max_pages=12)

    # Numéro : les formats usuels FACTURE N°, Invoice #, ou le numéro juste sous le titre FACTURE.
    num=_pdf_first([
        r'(?:facture|invoice)\s*(?:n(?:°|o)?|num(?:e|é)ro|#)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9._\-/]{2,})',
        r'\bFACTURE\s*\n\s*([A-Z0-9][A-Z0-9._\-/]{2,})',
    ], text)

    # Total TTC / net à payer / amount due. On privilégie les libellés explicites.
    amount_raw=_pdf_first([
        r'(?:total\s*ttc|net\s*(?:à|a)\s*payer|montant\s*(?:à|a)\s*payer|amount\s*due|total\s*due)\s*[:\-]?\s*([0-9][0-9\s.,]*\s*€?)',
        r'(?:total)\s*[:\-]?\s*([0-9][0-9\s.,]*\s*€)',
    ], text)
    amount=_pdf_money_value(amount_raw)

    issue_raw=_pdf_first([
        r'(?:date\s+d[’\']?émission|date\s+facture|invoice\s+date|date)\s*[:\-]?\s*([0-3]?\d[\/\-.][01]?\d[\/\-.](?:20)?\d{2})',
        r'(?:date\s+d[’\']?émission|date\s+facture|invoice\s+date|date)\s*[:\-]?\s*((?:20)?\d{2}[\/\-.][01]?\d[\/\-.][0-3]?\d)',
    ], text)
    due_raw=_pdf_first([
        r'(?:date\s+d[’\']?échéance|échéance|echeance|due\s+date)\s*[:\-]?\s*([0-3]?\d[\/\-.][01]?\d[\/\-.](?:20)?\d{2})',
        r'(?:date\s+d[’\']?échéance|échéance|echeance|due\s+date)\s*[:\-]?\s*((?:20)?\d{2}[\/\-.][01]?\d[\/\-.][0-3]?\d)',
    ], text)
    issue=parse_date(issue_raw) if issue_raw else None
    due=parse_date(due_raw) if due_raw else None

    # Client : section CLIENT/FACTURÉ À/BILL TO, première ligne utile qui suit.
    customer=None
    cm=re.search(r'(?:^|\n)\s*(?:CLIENT|FACTUR(?:É|E)\s*(?:À|A)|BILL\s*TO)\s*\n([^\n]+)',text,re.I)
    if cm:
        candidate=cm.group(1).strip(' :-')
        if candidate and not re.search(r'^(date|facture|invoice|adresse|email|téléphone|telephone)$',candidate,re.I):
            customer=candidate
    if not customer:
        customer=_pdf_first([
            r'(?:client|customer)\s*[:\-]\s*([^\n]{2,120})',
            r'(?:factur(?:é|e)\s*(?:à|a)|bill\s*to)\s*[:\-]\s*([^\n]{2,120})',
        ], text)

    email=_pdf_first([r'([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})'],text,re.I)
    phone=_pdf_first([r'(?<!\d)((?:\+33|0)\s*[1-9](?:[ .-]*\d{2}){4})(?!\d)'],text,re.I)

    missing=[]
    if not num: missing.append('numéro de facture')
    if not customer: missing.append('client')
    if amount is None or amount<=0: missing.append('montant TTC')
    if not due: missing.append('échéance')
    if missing:
        raise ValueError('Champs non détectés dans le PDF : '+', '.join(missing)+'. Vérifiez que ces informations sont écrites explicitement dans le document.')

    return {
        'invoice_number':num,
        'customer':customer,
        'amount':amount,
        'paid_amount':0.0,
        'issue_date':issue.isoformat() if issue else '',
        'due_date':due.isoformat(),
        'status':'unpaid',
        'type':'STANDARD',
        'retention_release_date':'',
        'customer_email':email or '',
        'customer_phone':phone or '',
    }


def _load_invoice_uploads(files):
    """Retourne un DataFrame uniforme pour CSV/XLSX ou un lot de PDF texte."""
    files=[f for f in files if f and f.filename]
    if not files:
        raise ValueError('Aucun fichier sélectionné.')
    exts={Path(secure_filename(f.filename)).suffix.lower() for f in files}
    if '.pdf' in exts:
        if exts != {'.pdf'}:
            raise ValueError('Sélectionnez soit un CSV/XLSX, soit uniquement des PDF dans le même import.')
        rows=[]
        errors=[]
        for f in files:
            path=None
            try:
                path,_=save_upload(f,'imports',FINANCIAL_IMPORT_EXTENSIONS)
                rows.append(_extract_invoice_pdf(path))
            except Exception as exc:
                errors.append(f"{secure_filename(f.filename)} : {exc}")
            finally:
                if path: cleanup_upload(path)
        if errors:
            preview=' | '.join(errors[:5])
            extra=f' (+{len(errors)-5} autre(s))' if len(errors)>5 else ''
            raise ValueError('Import PDF interrompu : '+preview+extra)
        if not rows:
            raise ValueError('Aucune facture PDF exploitable.')
        return pd.DataFrame(rows)

    if len(files)!=1:
        raise ValueError('Pour CSV/XLSX, sélectionnez un seul fichier.')
    path=None
    try:
        path,_=save_upload(files[0],'imports',FINANCIAL_IMPORT_EXTENSIONS)
        ext=path.suffix.lower()
        if ext in ('.xlsx','.xls'):
            return pd.read_excel(path)
        if ext=='.csv':
            return pd.read_csv(path)
        raise ValueError('Format de facture non pris en charge.')
    finally:
        if path: cleanup_upload(path)

def register(app):
    @app.route('/upload/bank-statement',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('uploads')
    def upload_bank_statement():
        """Rapprochement bancaire : propose des correspondances entre les lignes créditrices
        d'un relevé bancaire et les factures impayées. Ne marque JAMAIS rien comme payé
        automatiquement — l'utilisateur doit valider chaque correspondance sur l'écran suivant."""
        if request.method=='POST':
            f=request.files.get('file')
            if not f:
                flash('Aucun fichier sélectionné.'); return redirect(request.url)
            path=None
            try:
                path,_=save_upload(f,'imports',FINANCIAL_IMPORT_EXTENSIONS)
                ext=path.suffix.lower()
                if ext in ('.xlsx','.xls'):
                    df=pd.read_excel(path)
                elif ext=='.csv':
                    df=pd.read_csv(path)
                elif ext=='.pdf':
                    df=_extract_bank_statement_pdf(path)
                else:
                    raise ValueError('Format de relevé non pris en charge.')
            except Exception as e:
                flash(f'Import refusé : {e}'); return redirect(request.url)
            finally:
                if path: cleanup_upload(path)
            aliases={'date':['date','date operation',"date d'opération",'date valeur'],
                     'desc':['description','libelle','libellé','intitule','intitulé','détail','detail'],
                     'amount':['amount','montant','credit','crédit']}
            m=map_cols(df,aliases)
            missing=[k for k in ('date','amount') if k not in m]
            if missing:
                flash('Colonnes manquantes dans le relevé : '+', '.join(missing)); return redirect(request.url)

            c=cx()
            unpaid=c.execute("SELECT id,invoice_number,customer,MAX(amount-paid_amount,0) outstanding FROM invoices WHERE LOWER(COALESCE(status,''))!='paid'").fetchall()
            c.close()

            proposals=[]
            for _,row in df.iterrows():
                try: bank_amount=float(row[m['amount']])
                except Exception: continue
                if bank_amount<=0: continue  # on ne traite que les lignes créditrices (encaissements)
                bank_desc=str(row[m['desc']]) if 'desc' in m else ''
                bank_date=str(row[m['date']])[:10]
                candidates=[inv for inv in unpaid if abs(inv['outstanding']-bank_amount)<0.01]
                match=None
                if len(candidates)==1:
                    match=candidates[0]
                elif len(candidates)>1:
                    # Montant identique sur plusieurs factures : on tente de désambiguïser
                    # via le nom du client ou le n° de facture mentionné dans le libellé.
                    for cand in candidates:
                        if norm(cand['customer']) in norm(bank_desc) or norm(cand['invoice_number']) in norm(bank_desc):
                            match=cand; break
                proposals.append({'bank_date':bank_date,'bank_desc':bank_desc,'bank_amount':bank_amount,
                    'match':match,'ambiguous':len(candidates)>1 and match is None,'candidates':candidates})

            matched_count=sum(1 for p in proposals if p['match'])
            return render_template('bank_reconciliation.html',proposals=proposals,matched_count=matched_count,total_rows=len(proposals))

        return render_template('upload.html',title='Importer un relevé bancaire',kind='bank-statement')

    @app.route('/reconcile/confirm',methods=['POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('uploads')
    def reconcile_confirm():
        invoice_ids=request.form.getlist('confirm_invoice_id')
        if not invoice_ids:
            flash('Aucune correspondance sélectionnée.'); return redirect(url_for('upload_bank_statement'))
        count=0
        for iid in invoice_ids:
            try: iid=int(iid)
            except ValueError: continue
            c=cx()
            row=c.execute('SELECT * FROM invoices WHERE id=?',(iid,)).fetchone()
            if not row or norm(row['status'] or '')=='paid': c.close(); continue
            old=row['status']
            c.execute("UPDATE invoices SET status='paid' WHERE id=?",(iid,))
            c.commit(); c.close()  # commité avant log_status_change : évite un verrou SQLite
                                     # entre cette connexion et celle ouverte par log_status_change.
            count+=1
            log_status_change('INVOICE',iid,'RECOVER',old,'paid',note='Rapprochement bancaire')
        log_activity('BANK_RECONCILIATION',f'{count} facture(s) rapprochée(s) et marquée(s) payée(s)')
        flash(f'{count} facture(s) marquée(s) comme payée(s) suite au rapprochement bancaire.')
        return redirect(url_for('recover'))

    @app.route('/upload/invoices',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('uploads')
    def upload_invoices():
        if request.method=='POST':
            org=current_org()
            quota=quota_state('imports_per_month',organization_id=org['id'],plan=org['plan'])
            if not quota['allowed']:
                flash(
                    f"Quota mensuel d'imports atteint pour la formule {org['plan']} "
                    f"({quota['used']}/{quota['limit']}). Passez à une formule supérieure."
                )
                return redirect(request.url)

            files=request.files.getlist('file')
            if not files or not any(f and f.filename for f in files): return redirect(request.url)
            try:
                df=_load_invoice_uploads(files)
            except Exception as e:
                flash(f'Import refusé : {e}'); return redirect(request.url)

            aliases={'num':['invoice_number','invoice','number','numero','numéro','n° facture','facture'],'customer':['customer','client','customer_name','nom client'],'amount':['amount','montant','montant ttc','total'],'paid':['paid_amount','montant payé','montant paye'],'issue':['issue_date','date facture','date'],'due':['due_date','échéance','echeance',"date d'échéance"],'status':['status','statut','etat','état'],'itype':['type','nature','kind'],'release':['retention_release_date','date liberation','date de liberation','date de levée','date de levee','date liberation retenue'],'email':['customer_email','email','email client','courriel','mail client'],'phone':['customer_phone','telephone','téléphone','phone','mobile','portable']}
            m=map_cols(df,aliases)
            missing=[k for k in ('num','customer','amount','due') if k not in m]
            if missing:
                flash('Colonnes manquantes : '+', '.join(missing))
                return redirect(request.url)

            RETENTION_KEYWORDS=('retenue','retention','garantie')
            c=cx()
            previous_rows={r['invoice_number']:{'amount':r['amount'],'customer':r['customer']} for r in c.execute('SELECT invoice_number,amount,customer FROM invoices').fetchall()}
            c.execute('DELETE FROM invoices'); today=date.today(); signals=0; retentions=0
            ac_clean=auth_cx(); ac_clean.execute('DELETE FROM public_invoice_tokens WHERE organization_id=?',(org['id'],)); ac_clean.commit(); ac_clean.close()
            for _,r in df.iterrows():
                try:amount=float(r[m['amount']])
                except:continue
                paid=money(r[m['paid']]) if 'paid' in m else 0
                issue=parse_date(r[m['issue']]) if 'issue' in m else None
                status=str(r[m['status']]) if 'status' in m else ('paid' if paid>=amount else 'unpaid')
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
                score=invoice_score(amount,days,paid) if norm(status)!='paid' and days>0 else 0
                signals+=score>0
                email_val=str(r[m['email']]).strip() if 'email' in m and str(r[m['email']]).strip().lower()!='nan' else None
                phone_val=str(r[m['phone']]).strip() if 'phone' in m and str(r[m['phone']]).strip().lower()!='nan' else None
                c.execute('INSERT INTO invoices(invoice_number,customer,amount,paid_amount,issue_date,due_date,status,days_overdue,score,created_at,kind,retention_release_date,customer_email,customer_phone) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (str(r[m['num']]),str(r[m['customer']]),amount,paid,issue.isoformat() if issue else None,due.isoformat() if due else None,status,days,score,now(),kind,release.isoformat() if release else None,email_val,phone_val))
                local_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]
                token=register_public_invoice_token(org['id'],local_id)
                c.execute('UPDATE invoices SET public_token=? WHERE id=?',(token,local_id))
            c.commit()

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

            urgent=c.execute("SELECT COUNT(*) n,COALESCE(SUM(MAX(amount-paid_amount,0)),0) t FROM invoices WHERE LOWER(COALESCE(status,''))!='paid' AND days_overdue>0 AND score>=90").fetchone()
            overdue_rows=c.execute("SELECT customer,days_overdue,status FROM invoices WHERE LOWER(COALESCE(status,''))!='paid' AND days_overdue>0").fetchall()

            # Détection d'anomalies par rapport à l'import précédent : facture disparue,
            # ou montant qui a significativement changé pour le même numéro de facture.
            anomalies=[]
            if previous_rows:
                current_numbers={r['invoice_number'] for r in c.execute('SELECT invoice_number FROM invoices').fetchall()}
                for num,old in previous_rows.items():
                    if num not in current_numbers:
                        anomalies.append(f"Facture #{num} ({old['customer']}, {old['amount']:,.0f} €) présente à l'import précédent a disparu de ce nouvel import.")
                for r in c.execute('SELECT invoice_number,amount,customer FROM invoices').fetchall():
                    old=previous_rows.get(r['invoice_number'])
                    if old and old['amount'] and abs(r['amount']-old['amount'])/old['amount']>0.2:
                        anomalies.append(f"Facture #{r['invoice_number']} ({r['customer']}) : montant passé de {old['amount']:,.0f} € à {r['amount']:,.0f} €.")
            c.close()
            if anomalies:
                ac_log=auth_cx()
                for a in anomalies[:20]:
                    ac_log.execute('INSERT INTO activity_log(organization_id,user_id,event_type,description,created_at) VALUES(?,?,?,?,?)',
                        (org['id'],session.get('user_id'),'IMPORT_ANOMALY',a,now()))
                ac_log.commit(); ac_log.close()

            # Alimente les signaux partagés inter-organisations (anonymisés) : risque
            # acheteur croisé et benchmark sectoriel DSO. Best-effort, ne bloque jamais l'upload.
            try:
                sync_buyer_signals(org['id'],[dict(r) for r in overdue_rows])
                sync_sector_dso(org['id'],snap['avg_d'] or 0)
            except Exception:
                pass

            record_usage('imports_per_month',organization_id=org['id'])

            if urgent['n']:
                notify_org(f"🔴 ProfitOS · {urgent['n']} facture(s) en retard critique détectée(s) — {urgent['t']:,.0f} € à risque élevé.")
            msg=f'Factures analysées · {signals} signaux RECOVER.'
            if retentions: msg+=f' Dont {retentions} retenue(s) de garantie détectée(s).'
            flash(msg)
            if anomalies:
                flash(f"⚠ {len(anomalies)} anomalie(s) détectée(s) par rapport à l'import précédent — voir Settings → Activity Log pour le détail.")
            return redirect(url_for('recover'))

        return render_template('upload.html',title='Importer les factures',kind='invoices')

    RENEWABLE_CATEGORIES={
      'assurance':['assurance','insurance','décennale','decennale','responsabilite civile','responsabilité civile'],
      'energie':['energie','énergie','electricite','électricité','gaz','edf','engie','total energies'],
      'telecom':['telecom','téléphonie','telephonie','internet','fibre','abonnement mobile'],
      'logiciel':['logiciel','saas','abonnement logiciel','licence'],
    }

    def detect_duplicates(clean):
        out=[]
        counter=Counter((norm(v),round(a,2),d.isoformat() if d else '') for v,a,d,cat in clean)
        for (v,a,d),n in counter.items():
            if n>1:
                out.append(dict(title=f'Doublon potentiel — {v.title()}',value=a*(n-1),score=90,
                    details=f'{n} dépenses identiques détectées le {d}.',
                    reasons=['même fournisseur, montant et date'],warnings=['vérification humaine requise']))
        return out

    def detect_price_increases(clean):
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
        out=[]; today=date.today()
        by_vendor_cat=defaultdict(list)
        for v,a,d,cat in clean:
            fam=next((fam for fam,kws in RENEWABLE_CATEGORIES.items() if any(k in norm(cat) or k in norm(v) for k in kws)),None)
            if fam and d:by_vendor_cat[(fam,norm(v))].append((d,a))
        for (fam,v),rows in by_vendor_cat.items():
            rows.sort(); last_date,last_amount=rows[-1]; gap_days=(today-last_date).days
            if gap_days>=330 and len(rows)>=1:
                out.append(dict(title=f'Contrat à vérifier — {v.title()} ({fam})',value=round(last_amount,0),score=70,
                    details=f'Dernière dépense {fam} détectée il y a {gap_days} jours ({last_date.isoformat()}).',
                    reasons=[f'catégorie {fam} récurrente sans dépense récente'],
                    warnings=['obtenir 3 devis avant renouvellement automatique']))
        return out

    NON_OPTIMIZABLE_EXPENSE_MARKERS=(
        'urssaf','cotisations sociales','tva','impots et taxes','impôts et taxes',
        'salaires et paie','salaire','paie'
    )

    def _save_optimizable_expense(row):
        """Évite de présenter les obligations légales/sociales comme des économies négociables.

        Les doublons restent contrôlés séparément : un double prélèvement URSSAF/TVA peut
        réellement mériter une vérification, mais on ne génère ni hausse fournisseur ni
        renouvellement de contrat sur ces catégories.
        """
        v,a,d,cat=row
        text=norm(f'{cat or ""} {v or ""}')
        return not any(marker in text for marker in NON_OPTIMIZABLE_EXPENSE_MARKERS)

    def run_save_engine(clean):
        optimizable=[row for row in clean if _save_optimizable_expense(row)]
        return detect_duplicates(clean)+detect_price_increases(optimizable)+detect_stale_contracts(optimizable)

    @app.route('/upload/expenses',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('uploads')
    def upload_expenses():
        if request.method=='POST':
            org=current_org()
            quota=quota_state('imports_per_month',organization_id=org['id'],plan=org['plan'])
            if not quota['allowed']:
                flash(
                    f"Quota mensuel d'imports atteint pour la formule {org['plan']} "
                    f"({quota['used']}/{quota['limit']}). Passez à une formule supérieure."
                )
                return redirect(request.url)

            f=request.files.get('file')
            if not f:return redirect(request.url)
            path=None
            try:
                path, original_name=save_upload(f,'imports',FINANCIAL_IMPORT_EXTENSIONS)
                ext=path.suffix.lower()
                if ext in ('.xlsx','.xls'):
                    df=pd.read_excel(path)
                elif ext=='.csv':
                    df=pd.read_csv(path)
                elif ext=='.pdf':
                    df=pd.DataFrame([_extract_expense_pdf(path)])
                else:
                    raise ValueError('Format de dépense non pris en charge.')
            except Exception as e:
                flash(f'Import refusé : {e}'); return redirect(request.url)
            finally:
                if path: cleanup_upload(path)

            aliases={'vendor':['vendor','supplier','fournisseur'],'desc':['description','libelle','libellé'],'amount':['amount','montant','total'],'date':['date','expense_date','date dépense','date depense'],'cat':['category','categorie','catégorie']}
            m=map_cols(df,aliases)
            missing=[k for k in ('vendor','amount','date') if k not in m]
            if missing:
                flash('Colonnes manquantes : '+', '.join(missing))
                return redirect(request.url)

            c=cx()
            # Imports cumulatifs : conserver les dépenses existantes et éviter
            # de réinsérer un doublon strict lors d'un nouvel import.
            for _,r in df.iterrows():
                try:a=float(r[m['amount']])
                except:continue
                d=parse_date(r[m['date']]); v=str(r[m['vendor']]).strip(); desc=str(r[m['desc']]) if 'desc' in m else ''; cat=str(r[m['cat']]) if 'cat' in m else ''
                d_iso=d.isoformat() if d else None
                exists=c.execute(
                    '''SELECT 1 FROM expenses
                       WHERE COALESCE(vendor,'')=? AND COALESCE(description,'')=?
                         AND amount=? AND COALESCE(expense_date,'')=COALESCE(?, '')
                         AND COALESCE(category,'')=? LIMIT 1''',
                    (v,desc,a,d_iso,cat)
                ).fetchone()
                if not exists:
                    c.execute('INSERT INTO expenses(vendor,description,amount,expense_date,category) VALUES(?,?,?,?,?)',(v,desc,a,d_iso,cat))

            # SAVE est recalculé sur tout l'historique conservé.
            c.execute("DELETE FROM opportunities WHERE type='SAVE'")
            clean=[]
            for er in c.execute('SELECT vendor,amount,expense_date,category FROM expenses').fetchall():
                clean.append((er['vendor'],float(er['amount'] or 0),parse_date(er['expense_date']),er['category'] or ''))

            for opp in run_save_engine(clean):
                c.execute("INSERT INTO opportunities(type,title,value,score,details,source,reasons,warnings,status,created_at) VALUES('SAVE',?,?,?,?,?,?,?,'OPEN',?)",
                    (opp['title'],opp['value'],opp['score'],opp['details'],'Expense Engine',
                     json.dumps(opp['reasons'],ensure_ascii=False),json.dumps(opp['warnings'],ensure_ascii=False),now()))
            c.commit(); c.close()

            record_usage('imports_per_month',organization_id=org['id'])

            flash('Dépenses analysées.')
            return redirect(url_for('save'))

        return render_template('upload.html',title='Importer les dépenses',kind='expenses')

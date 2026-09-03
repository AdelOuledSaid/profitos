from datetime import timedelta
import uuid
from profitos.runtime import *
from profitos.plan_usage import quota_state, record_usage
from profitos.feature_access import requires_paid_plan


def _compute_line_items(form):
    """Lit jusqu'à 8 lignes de facture depuis le formulaire (libellé, quantité, prix
    unitaire, taux de TVA). Les lignes vides sont ignorées — pas de JS dynamique
    nécessaire, conforme à la politique de sécurité stricte de l'app (pas de script
    inline)."""
    items=[]
    for i in range(1,9):
        label=form.get(f'label_{i}','').strip()
        if not label: continue
        try: qty=float(form.get(f'qty_{i}','1').replace(',','.'))
        except ValueError: qty=1
        try: unit_price=float(form.get(f'price_{i}','0').replace(',','.'))
        except ValueError: unit_price=0
        try: vat_rate=float(form.get(f'vat_{i}','20').replace(',','.'))
        except ValueError: vat_rate=20
        line_total=qty*unit_price
        items.append({'label':label,'qty':qty,'unit_price':unit_price,'vat_rate':vat_rate,'line_total':line_total})
    return items


def _totals(items):
    subtotal=sum(i['line_total'] for i in items)
    vat_amount=sum(i['line_total']*i['vat_rate']/100 for i in items)
    return subtotal,vat_amount,subtotal+vat_amount



def _display_status(inv):
    status=inv['status']
    if status=='sent' and inv['due_date']:
        try:
            if date.fromisoformat(inv['due_date']) < date.today():
                return 'overdue'
        except (TypeError,ValueError):
            pass
    return status


def _next_invoice_number(c):
    year=datetime.now(timezone.utc).year
    prefix=f"FA-{year}-"
    rows=c.execute("SELECT invoice_number FROM outgoing_invoices WHERE invoice_number LIKE ?",(prefix+'%',)).fetchall()
    highest=0
    for row in rows:
        try:
            highest=max(highest,int((row['invoice_number'] or '').rsplit('-',1)[1]))
        except (ValueError,IndexError):
            pass
    candidate=highest+1
    while c.execute("SELECT 1 FROM outgoing_invoices WHERE invoice_number=?",(f"{prefix}{candidate:03d}",)).fetchone():
        candidate+=1
    return f"{prefix}{candidate:03d}"


def _next_credit_number(c):
    year=datetime.now(timezone.utc).year
    prefix=f"AV-{year}-"
    rows=c.execute("SELECT credit_number FROM outgoing_credit_notes WHERE credit_number LIKE ?",(prefix+'%',)).fetchall()
    highest=0
    for row in rows:
        try:
            highest=max(highest,int((row['credit_number'] or '').rsplit('-',1)[1]))
        except (ValueError,IndexError):
            pass
    return f"{prefix}{highest+1:03d}"


def _credited_total(c, invoice_id):
    row=c.execute("SELECT COALESCE(SUM(total),0) AS n FROM outgoing_credit_notes WHERE original_invoice_id=? AND status='issued'",(invoice_id,)).fetchone()
    return float(row['n'] or 0)


def _next_quote_number(c):
    year=datetime.now(timezone.utc).year
    prefix=f"DEV-{year}-"
    rows=c.execute("SELECT quote_number FROM outgoing_quotes WHERE quote_number LIKE ?",(prefix+'%',)).fetchall()
    highest=0
    for row in rows:
        try:
            highest=max(highest,int((row['quote_number'] or '').rsplit('-',1)[1]))
        except (ValueError,IndexError):
            pass
    return f"{prefix}{highest+1:03d}"


def _quote_status_label(status):
    return {'draft':'Brouillon','sent':'Envoyé','accepted':'Accepté','refused':'Refusé','converted':'Facturé'}.get(status,status)


_PURCHASE_PDF_MAX_BYTES = 5 * 1024 * 1024

def _purchase_money(raw):
    if raw is None: return None
    v=str(raw).replace('\u00a0',' ').replace('€','').strip()
    v=re.sub(r'[^0-9,\.\- ]','',v).replace(' ','')
    if ',' in v and '.' in v:
        v=v.replace('.','').replace(',','.') if v.rfind(',')>v.rfind('.') else v.replace(',','')
    elif ',' in v: v=v.replace(',','.')
    try: return float(v)
    except Exception: return None


def _purchase_pdf_date(raw):
    if not raw:
        return None
    v=str(raw).strip()
    # ISO: YYYY-MM-DD / YYYY.MM.DD / YYYY/MM/DD
    m=re.fullmatch(r'(\d{4})[\/\-.](\d{1,2})[\/\-.](\d{1,2})',v)
    if m:
        try: return date(int(m.group(1)),int(m.group(2)),int(m.group(3)))
        except ValueError: return None
    # French invoices: DD/MM/YYYY (also accepts - and .)
    m=re.fullmatch(r'(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{2}|\d{4})',v)
    if m:
        year=int(m.group(3))
        if year < 100: year += 2000
        try: return date(year,int(m.group(2)),int(m.group(1)))
        except ValueError: return None
    return None

def _purchase_pdf_extract(path):
    reader=PdfReader(str(path))
    text='\n'.join((page.extract_text() or '') for page in reader.pages[:12]).strip()
    if len(text)<20:
        raise ValueError("PDF sans texte exploitable. Les PDF scannés ne sont pas encore pris en charge.")

    def first(patterns):
        for pat in patterns:
            m=re.search(pat,text,re.I|re.M)
            if m: return m.group(1).strip()
        return None

    number=first([
        r'(?:facture|invoice)\s*(?:n(?:°|o)?|num(?:e|é)ro|#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9._\-/]{2,})',
        r'(?:n(?:°|o)?\s*(?:de\s+)?facture|num(?:e|é)ro\s+(?:de\s+)?facture|invoice\s*(?:number|no\.?))\s*[:\-]?\s*\n?\s*([A-Z0-9][A-Z0-9._\-/]{2,})'
    ])
    issue_raw=first([
        r'(?:date\s+d[’\']?émission|date\s+facture|invoice\s+date|date)\s*[:\-]?\s*([0-3]?\d[\/\-.][01]?\d[\/\-.](?:20)?\d{2})',
        r'(?:date\s+d[’\']?émission|date\s+facture|invoice\s+date|date)\s*[:\-]?\s*((?:20)?\d{2}[\/\-.][01]?\d[\/\-.][0-3]?\d)'
    ])
    due_raw=first([
        r'(?:date\s+d[’\']?échéance|échéance|echeance|due\s+date)\s*[:\-]?\s*([0-3]?\d[\/\-.][01]?\d[\/\-.](?:20)?\d{2})',
        r'(?:date\s+d[’\']?échéance|échéance|echeance|due\s+date)\s*[:\-]?\s*((?:20)?\d{2}[\/\-.][01]?\d[\/\-.][0-3]?\d)'
    ])
    total=_purchase_money(first([
        r'(?:total\s*ttc|net\s*(?:à|a)\s*payer|amount\s*due|total\s*due)\s*[:\-]?\s*([0-9][0-9\s.,]*\s*€?)'
    ]))
    subtotal=_purchase_money(first([
        r'(?:sous[\-\s]?total\s*ht|total\s*ht|montant\s*ht|subtotal)\s*[:\-]?\s*([0-9][0-9\s.,]*\s*€?)'
    ]))
    vat=_purchase_money(first([
        r'(?:montant\s*)?tva(?:\s*\([^)]*\))?\s*[:\-]?\s*([0-9][0-9\s.,]*\s*€?)',
        r'(?:vat)\s*[:\-]?\s*([0-9][0-9\s.,]*\s*€?)'
    ]))
    if subtotal is None and total is not None and vat is not None: subtotal=round(total-vat,2)
    if vat is None and total is not None and subtotal is not None: vat=round(total-subtotal,2)

    vendor=first([
        r'(?:fournisseur|supplier|vendor|émis\s+par|emis\s+par|issued\s+by)\s*[:\-]\s*([^\n]{2,120})'
    ])
    if not vendor:
        for line in [x.strip() for x in text.splitlines()[:18] if x.strip()]:
            if len(line)<2 or len(line)>100: continue
            if re.search(r'^(facture|invoice|date|échéance|echeance|total|tva|client|description)\b',line,re.I): continue
            if re.fullmatch(r'[0-9\s.,€+\-/]+',line): continue
            vendor=line; break

    issue=_purchase_pdf_date(issue_raw) if issue_raw else None
    due=_purchase_pdf_date(due_raw) if due_raw else None
    if issue_raw and not issue:
        raise ValueError("Date de facture détectée mais illisible. Vérifiez-la manuellement.")
    if due_raw and not due:
        raise ValueError("Date d'échéance détectée mais illisible. Vérifiez-la manuellement.")

    missing=[]
    if not vendor: missing.append("fournisseur")
    if not number: missing.append("numéro")
    if subtotal is None: missing.append("HT")
    if vat is None: missing.append("TVA")
    if total is None: missing.append("TTC")
    if missing:
        raise ValueError("Champs non détectés : "+", ".join(missing)+". Corrigez le PDF ou saisissez la facture manuellement.")
    if abs(round(subtotal+vat-total,2))>0.02:
        raise ValueError("Les montants HT + TVA ne correspondent pas au TTC. Import refusé par sécurité.")
    return {'supplier_name':vendor,'invoice_number':number,
            'issue_date':issue.isoformat() if issue else '',
            'due_date':due.isoformat() if due else '',
            'subtotal':subtotal,'vat_amount':vat,'total':total}


def _purchase_pdf_dir():
    org_id = session.get('org_id')
    if not org_id:
        raise RuntimeError("Organisation non sélectionnée")
    root = UP / "purchase_documents" / str(int(org_id))
    root.mkdir(parents=True, exist_ok=True)
    return root

def _save_purchase_pdf(uploaded):
    if not uploaded or not uploaded.filename:
        return None
    if not uploaded.filename.lower().endswith(".pdf"):
        raise ValueError("Le justificatif doit être un fichier PDF.")
    data = uploaded.read(_PURCHASE_PDF_MAX_BYTES + 1)
    if len(data) > _PURCHASE_PDF_MAX_BYTES:
        raise ValueError("Le PDF dépasse la taille maximale de 5 Mo.")
    if not data.startswith(b"%PDF-"):
        raise ValueError("Le fichier envoyé n'est pas un PDF valide.")
    stored = uuid.uuid4().hex + ".pdf"
    (_purchase_pdf_dir() / stored).write_bytes(data)
    return stored


def register(app):
    @app.route('/facturation/clients')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_clients():
        c=cx()
        rows=c.execute("""SELECT cl.*,
          (SELECT COUNT(*) FROM outgoing_invoices i WHERE lower(i.client_name)=lower(cl.name)) invoice_count,
          (SELECT COALESCE(SUM(i.total),0) FROM outgoing_invoices i WHERE lower(i.client_name)=lower(cl.name) AND i.status='paid') paid_total
          FROM invoicing_clients cl ORDER BY lower(cl.name)""").fetchall()
        c.close()
        return render_template('invoicing_clients.html',rows=rows)

    @app.route('/facturation/clients/nouveau',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_client_new():
        if request.method=='POST':
            name=request.form.get('name','').strip()
            if not name:
                flash("Le nom du client est requis.")
                return redirect(url_for('invoicing_client_new'))
            c=cx()
            c.execute("""INSERT INTO invoicing_clients(name,email,address,siret,vat_number,phone,notes,created_at,updated_at)
                         VALUES(?,?,?,?,?,?,?,?,?)""",
              (name,request.form.get('email','').strip(),request.form.get('address','').strip(),
               request.form.get('siret','').strip(),request.form.get('vat_number','').strip(),
               request.form.get('phone','').strip(),request.form.get('notes','').strip(),now(),now()))
            c.commit(); client_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.close()
            flash(f"Client {name} créé.")
            return redirect(url_for('invoicing_client_detail',client_id=client_id))
        return render_template('invoicing_client_form.html')

    @app.route('/facturation/clients/<int:client_id>',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_client_detail(client_id):
        c=cx()
        client=c.execute('SELECT * FROM invoicing_clients WHERE id=?',(client_id,)).fetchone()
        if not client: c.close(); abort(404)
        if request.method=='POST':
            name=request.form.get('name','').strip()
            if not name:
                c.close(); flash("Le nom du client est requis.")
                return redirect(url_for('invoicing_client_detail',client_id=client_id))
            c.execute("""UPDATE invoicing_clients SET name=?,email=?,address=?,siret=?,vat_number=?,phone=?,notes=?,updated_at=? WHERE id=?""",
              (name,request.form.get('email','').strip(),request.form.get('address','').strip(),
               request.form.get('siret','').strip(),request.form.get('vat_number','').strip(),
               request.form.get('phone','').strip(),request.form.get('notes','').strip(),now(),client_id))
            c.commit(); client=c.execute('SELECT * FROM invoicing_clients WHERE id=?',(client_id,)).fetchone()
            flash("Fiche client mise à jour.")
        invoices=c.execute("SELECT * FROM outgoing_invoices WHERE lower(client_name)=lower(?) ORDER BY id DESC",(client['name'],)).fetchall()
        credits=c.execute("SELECT * FROM outgoing_credit_notes WHERE lower(client_name)=lower(?) ORDER BY id DESC",(client['name'],)).fetchall()
        c.close()
        return render_template('invoicing_client_detail.html',client=client,invoices=invoices,credits=credits)

    @app.route('/facturation/devis')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_quotes():
        c=cx()
        rows=c.execute("SELECT * FROM outgoing_quotes ORDER BY id DESC").fetchall()
        c.close()
        return render_template('invoicing_quotes.html',rows=rows,quote_status_label=_quote_status_label)

    @app.route('/facturation/devis/nouveau',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_quote_new():
        c=cx()
        company_row=c.execute('SELECT * FROM company WHERE id=1').fetchone()
        clients=c.execute("SELECT * FROM invoicing_clients ORDER BY lower(name)").fetchall()
        if request.method=='POST':
            client_name=request.form.get('client_name','').strip()
            if not client_name:
                c.close(); flash("Le nom du client est requis.")
                return redirect(url_for('invoicing_quote_new'))
            items=_compute_line_items(request.form)
            if not items:
                c.close(); flash("Au moins une ligne de devis est requise.")
                return redirect(url_for('invoicing_quote_new'))
            subtotal=round(sum(x['line_total'] for x in items),2)
            vat_amount=round(sum(x['line_total']*x['vat_rate']/100 for x in items),2)
            total=round(subtotal+vat_amount,2)
            quote_number=_next_quote_number(c)
            c.execute("""INSERT INTO outgoing_quotes
              (quote_number,client_name,client_address,client_email,issue_date,valid_until,line_items,
               subtotal,vat_amount,total,notes,status,created_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,'draft',?)""",
              (quote_number,client_name,request.form.get('client_address','').strip(),
               request.form.get('client_email','').strip(),date.today().isoformat(),
               request.form.get('valid_until') or None,json.dumps(items,ensure_ascii=False),
               subtotal,vat_amount,total,request.form.get('notes','').strip(),now()))
            c.commit()
            quote_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.close()
            quote_token=secrets.token_urlsafe(24)
            ac=auth_cx()
            ac.execute('INSERT INTO outgoing_quote_tokens(token,organization_id,quote_local_id,created_at) VALUES(?,?,?,?)',
                       (quote_token,session['org_id'],quote_id,now()))
            ac.commit(); ac.close()
            log_activity('QUOTE_CREATED',f"Devis {quote_number} créé en brouillon")
            flash(f"Devis {quote_number} créé en brouillon.")
            return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))
        c.close()
        return render_template('invoicing_quote_new.html',company=company_row,clients=clients,today=date.today().isoformat())

    @app.route('/facturation/devis/<int:quote_id>')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def invoicing_quote_detail(quote_id):
        c=cx(); q=c.execute('SELECT * FROM outgoing_quotes WHERE id=?',(quote_id,)).fetchone(); c.close()
        if not q: abort(404)
        return render_template('invoicing_quote_detail.html',q=q,items=json.loads(q['line_items'] or '[]'),
                               status_label=_quote_status_label(q['status']))

    @app.post('/facturation/devis/<int:quote_id>/envoyer')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_quote_send(quote_id):
        c=cx()
        q=c.execute('SELECT * FROM outgoing_quotes WHERE id=?',(quote_id,)).fetchone()
        if not q:
            c.close(); abort(404)
        if q['status'] not in ('draft','sent'):
            c.close()
            flash("Ce devis ne peut plus être envoyé.")
            return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))
        if not q['client_email']:
            c.close()
            flash("Aucun email client renseigné pour ce devis.")
            return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))

        ac=auth_cx()
        mapping=ac.execute(
            'SELECT * FROM outgoing_quote_tokens WHERE organization_id=? AND quote_local_id=?',
            (session['org_id'],quote_id)
        ).fetchone()
        if mapping:
            quote_token=mapping['token']
        else:
            quote_token=secrets.token_urlsafe(24)
            ac.execute(
                'INSERT INTO outgoing_quote_tokens(token,organization_id,quote_local_id,created_at) VALUES(?,?,?,?)',
                (quote_token,session['org_id'],quote_id,now())
            )
            ac.commit()
        ac.close()

        org=current_org()
        base=os.environ.get('APP_BASE_URL',request.host_url.rstrip('/'))
        link=f"{base}{url_for('public_quote_view',token=quote_token)}"
        html=render_template(
            'email_transactional.html',
            title=f"Devis {q['quote_number']} — {org['name']}",
            intro=f"Voici votre devis {q['quote_number']} de {org['name']}, d'un montant de {q['total']:,.2f} € TTC.",
            cta_label='Consulter et répondre au devis',
            cta_url=link,
            footer="Vous pouvez accepter ou refuser ce devis depuis la page sécurisée."
        )
        result=send_email(q['client_email'],f"Devis {q['quote_number']} — {org['name']}",html)

        if result.get('dry_run'):
            flash(f"Service email non configuré — devis non envoyé réellement (mode simulation) à {q['client_email']}.")
        elif result.get('sent'):
            c.execute("UPDATE outgoing_quotes SET status='sent',sent_at=? WHERE id=?",(now(),quote_id))
            c.commit()
            log_activity('QUOTE_SENT',f"Devis {q['quote_number']} envoyé à {q['client_email']}")
            flash(f"Devis envoyé à {q['client_email']}.")
        else:
            flash(f"Échec de l'envoi : {result.get('error','erreur inconnue')}")
        c.close()
        return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))

    @app.post('/facturation/devis/<int:quote_id>/accepter')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_quote_accept(quote_id):
        c=cx()
        q=c.execute('SELECT * FROM outgoing_quotes WHERE id=?',(quote_id,)).fetchone()
        if not q:
            c.close(); abort(404)
        if q['status']!='sent':
            c.close()
            flash("La réponse à ce devis est déjà enregistrée et verrouillée.")
            return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))
        c.execute("UPDATE outgoing_quotes SET status='accepted',accepted_at=? WHERE id=?",(now(),quote_id))
        c.commit(); c.close()
        log_activity('QUOTE_ACCEPTED',f"Devis {q['quote_number']} accepté")
        flash(f"Devis {q['quote_number']} accepté.")
        return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))

    @app.post('/facturation/devis/<int:quote_id>/refuser')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_quote_refuse(quote_id):
        c=cx()
        q=c.execute('SELECT * FROM outgoing_quotes WHERE id=?',(quote_id,)).fetchone()
        if not q:
            c.close(); abort(404)
        if q['status']!='sent':
            c.close()
            flash("La réponse à ce devis est déjà enregistrée et verrouillée.")
            return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))
        c.execute("UPDATE outgoing_quotes SET status='refused',refused_at=? WHERE id=?",(now(),quote_id))
        c.commit(); c.close()
        log_activity('QUOTE_REFUSED',f"Devis {q['quote_number']} refusé")
        flash(f"Devis {q['quote_number']} refusé.")
        return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))

    @app.post('/facturation/devis/<int:quote_id>/convertir')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_quote_convert(quote_id):
        c=cx(); q=c.execute('SELECT * FROM outgoing_quotes WHERE id=?',(quote_id,)).fetchone()
        if not q: c.close(); abort(404)
        if q['status']!='accepted' or q['converted_invoice_id']:
            c.close(); flash("Seul un devis accepté et non encore facturé peut être converti.")
            return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))
        invoice_number=_next_invoice_number(c)
        token=secrets.token_urlsafe(24)
        due=(date.today()+timedelta(days=30)).isoformat()
        c.execute("""INSERT INTO outgoing_invoices
          (invoice_number,client_name,client_address,client_email,issue_date,due_date,line_items,
           subtotal,vat_amount,total,notes,status,public_token,created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,'draft',?,?)""",
          (invoice_number,q['client_name'],q['client_address'],q['client_email'],date.today().isoformat(),due,
           q['line_items'],q['subtotal'],q['vat_amount'],q['total'],
           f"Créée depuis le devis {q['quote_number']}." + (("\\n"+q['notes']) if q['notes'] else ""),
           token,now()))
        c.commit()
        invoice_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]
        c.execute("UPDATE outgoing_quotes SET status='converted',converted_invoice_id=? WHERE id=?",(invoice_id,quote_id))
        c.commit(); c.close()
        log_activity('QUOTE_CONVERTED',f"Devis {q['quote_number']} converti en {invoice_number}")
        flash(f"Devis {q['quote_number']} converti en facture {invoice_number}.")
        return redirect(url_for('invoicing_detail',invoice_id=invoice_id))

    @app.route('/facturation/devis/<int:quote_id>/pdf')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def invoicing_quote_pdf(quote_id):
        c=cx()
        q=c.execute('SELECT * FROM outgoing_quotes WHERE id=?',(quote_id,)).fetchone()
        company_row=c.execute('SELECT * FROM company WHERE id=1').fetchone()
        c.close()
        if not q: abort(404)
        pdf_bytes=_render_quote_pdf(q,company_row)
        if pdf_bytes is None:
            flash("La génération PDF nécessite le paquet 'fpdf2'.")
            return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))
        return Response(pdf_bytes,mimetype='application/pdf',
          headers={'Content-Disposition':f'attachment; filename="{q["quote_number"]}.pdf"'})

    @app.get('/facturation/creances')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_receivables():
        c=cx()
        rows=c.execute("""
            SELECT i.*,
                   COALESCE((SELECT SUM(cn.total)
                             FROM outgoing_credit_notes cn
                             WHERE cn.original_invoice_id=i.id
                               AND cn.status='issued'),0) AS credited_total
            FROM outgoing_invoices i
            WHERE i.status='sent'
            ORDER BY i.due_date ASC, i.id DESC
        """).fetchall()

        today=date.today()
        invoices=[]
        by_client={}
        total_due=0.0
        overdue_due=0.0

        for r in rows:
            total=float(r['total'] or 0)
            credited=float(r['credited_total'] or 0)
            remaining=max(0.0,total-credited)
            if remaining <= 0.01:
                continue

            due=None
            try:
                due=datetime.strptime(r['due_date'],'%Y-%m-%d').date() if r['due_date'] else None
            except Exception:
                due=None
            overdue=bool(due and due < today)
            days_overdue=(today-due).days if overdue else 0
            total_due += remaining
            if overdue:
                overdue_due += remaining

            item={
                'id':r['id'],
                'invoice_number':r['invoice_number'],
                'client_name':r['client_name'],
                'client_email':r['client_email'],
                'due_date':r['due_date'],
                'remaining':remaining,
                'overdue':overdue,
                'days_overdue':days_overdue,
            }
            invoices.append(item)

            client=(r['client_name'] or 'Client sans nom').strip()
            agg=by_client.setdefault(client,{'client_name':client,'total':0.0,'overdue':0.0,'count':0})
            agg['total'] += remaining
            agg['count'] += 1
            if overdue:
                agg['overdue'] += remaining

        clients=sorted(by_client.values(),key=lambda x:(x['overdue'],x['total']),reverse=True)
        c.close()
        return render_template(
            'invoicing_receivables.html',
            invoices=invoices,
            clients=clients,
            total_due=total_due,
            overdue_due=overdue_due,
            unpaid_count=len(invoices),
            overdue_count=sum(1 for x in invoices if x['overdue'])
        )

    @app.get('/facturation/achats')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_list():
        c=cx()
        purchases=c.execute("SELECT * FROM purchase_invoices ORDER BY due_date ASC, id DESC").fetchall()
        suppliers=c.execute("SELECT * FROM suppliers ORDER BY name ASC").fetchall()
        today=date.today()
        total_unpaid=0.0
        total_overdue=0.0
        view=[]
        for p in purchases:
            overdue=False
            if p['status']=='unpaid' and p['due_date']:
                try:
                    overdue=datetime.strptime(p['due_date'],'%Y-%m-%d').date() < today
                except Exception:
                    overdue=False
            if p['status']=='unpaid':
                total_unpaid += float(p['total'] or 0)
                if overdue:
                    total_overdue += float(p['total'] or 0)
            view.append({'row':p,'overdue':overdue})
        c.close()
        return render_template('purchase_list.html',purchases=view,suppliers=suppliers,
                               total_unpaid=total_unpaid,total_overdue=total_overdue)

    @app.route('/facturation/achats/pilotage')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_analytics():
        """Pilotage des dépenses fournisseurs — uniquement des données réelles issues
        de purchase_invoices, aucun chiffre inventé. Filtre par période via ?date_from
        et ?date_to (format AAAA-MM-JJ)."""
        date_from=request.args.get('date_from','').strip()
        date_to=request.args.get('date_to','').strip()

        c=cx()
        query="SELECT * FROM purchase_invoices WHERE 1=1"
        params=[]
        if date_from:
            query+=" AND issue_date>=?"; params.append(date_from)
        if date_to:
            query+=" AND issue_date<=?"; params.append(date_to)
        rows=c.execute(query,params).fetchall()
        c.close()

        total_ht=sum(r['subtotal'] or 0 for r in rows)
        total_vat=sum(r['vat_amount'] or 0 for r in rows)
        total_ttc=sum(r['total'] or 0 for r in rows)
        invoice_count=len(rows)

        # Dépenses par fournisseur, triées par montant décroissant.
        by_supplier={}
        for r in rows:
            name=r['supplier_name'] or 'Fournisseur inconnu'
            by_supplier.setdefault(name,{'ht':0.0,'vat':0.0,'ttc':0.0,'count':0})
            by_supplier[name]['ht']+=r['subtotal'] or 0
            by_supplier[name]['vat']+=r['vat_amount'] or 0
            by_supplier[name]['ttc']+=r['total'] or 0
            by_supplier[name]['count']+=1
        supplier_rows=sorted(
            [{'name':k,**v} for k,v in by_supplier.items()],
            key=lambda x:x['ttc'],reverse=True
        )
        top_suppliers=supplier_rows[:8]

        # Dépenses par mois (AAAA-MM), triées chronologiquement.
        by_month={}
        for r in rows:
            if not r['issue_date']: continue
            month=r['issue_date'][:7]
            by_month[month]=by_month.get(month,0.0)+(r['total'] or 0)
        months_sorted=sorted(by_month.keys())
        monthly_series=[(m,round(by_month[m])) for m in months_sorted]
        evolution_chart=bars_svg(monthly_series[-12:]) if len(monthly_series)>=2 else None
        top_suppliers_chart=bars_svg([(s['name'][:12],round(s['ttc'])) for s in top_suppliers[:6]]) if top_suppliers else None

        return render_template('purchase_analytics.html',
            total_ht=total_ht,total_vat=total_vat,total_ttc=total_ttc,invoice_count=invoice_count,
            supplier_rows=supplier_rows,top_suppliers=top_suppliers,monthly_series=monthly_series,
            evolution_chart=evolution_chart,top_suppliers_chart=top_suppliers_chart,
            date_from=date_from,date_to=date_to)

    @app.route('/facturation/achats/export')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_export():
        """Export comptable des factures fournisseurs (CSV/Excel) — même mécanisme et
        même quota mensuel que les autres exports de l'app (RECOVER, etc.)."""
        org=current_org()
        quota=quota_state('reports_per_month',organization_id=org['id'],plan=org['plan'])
        if not quota['allowed']:
            flash(f"Quota mensuel d'exports atteint pour la formule {org['plan']} ({quota['used']}/{quota['limit']}). Passez à une formule supérieure.")
            return redirect(url_for('purchase_list'))
        c=cx()
        purchases=c.execute("SELECT * FROM purchase_invoices ORDER BY due_date ASC, id DESC").fetchall()
        c.close()
        data=[{'Fournisseur':p['supplier_name'],'N° facture':p['invoice_number'],
               "Date d'émission":p['issue_date'] or '',"Date d'échéance":p['due_date'] or '',
               'Montant HT (€)':round(p['subtotal'] or 0,2),'TVA (€)':round(p['vat_amount'] or 0,2),
               'Montant TTC (€)':round(p['total'] or 0,2),
               'Statut':'Payée' if p['status']=='paid' else 'À payer'} for p in purchases]
        record_usage('reports_per_month',organization_id=org['id'])
        return export_response(data,'profitos-achats-fournisseurs')

    @app.route('/facturation/fournisseurs/nouveau',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def supplier_new():
        if request.method=='POST':
            name=(request.form.get('name') or '').strip()
            if not name:
                flash("Le nom du fournisseur est obligatoire.")
                return redirect(url_for('supplier_new'))
            c=cx()
            c.execute("""INSERT INTO suppliers(name,email,phone,address,siret,vat_number,notes,created_at)
                         VALUES(?,?,?,?,?,?,?,?)""",
                      (name,(request.form.get('email') or '').strip(),
                       (request.form.get('phone') or '').strip(),
                       (request.form.get('address') or '').strip(),
                       (request.form.get('siret') or '').strip(),
                       (request.form.get('vat_number') or '').strip(),
                       (request.form.get('notes') or '').strip(),now()))
            c.commit(); c.close()
            flash("Fournisseur ajouté.")
            return redirect(url_for('purchase_list'))
        return render_template('supplier_new.html')

    @app.post('/facturation/achats/importer-pdf')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_import_pdf():
        uploaded=request.files.get('document')
        try:
            stored=_save_purchase_pdf(uploaded)
            if not stored:
                raise ValueError("Sélectionnez un fichier PDF.")
            path=_purchase_pdf_dir()/stored
            detected=_purchase_pdf_extract(path)
        except ValueError as e:
            if 'stored' in locals() and stored:
                try: (_purchase_pdf_dir()/stored).unlink(missing_ok=True)
                except OSError: pass
            flash(str(e))
            return redirect(url_for('purchase_new'))

        c=cx()
        suppliers=c.execute("SELECT * FROM suppliers ORDER BY name ASC").fetchall()
        c.close()
        flash("PDF analysé. Vérifiez les informations avant d'enregistrer.")
        return render_template('purchase_new.html',suppliers=suppliers,
                               detected=detected,pending_document=stored)

    @app.route('/facturation/achats/nouvelle',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_new():
        c=cx()
        suppliers=c.execute("SELECT * FROM suppliers ORDER BY name ASC").fetchall()
        if request.method=='POST':
            supplier_id=request.form.get('supplier_id') or None
            supplier_name=(request.form.get('supplier_name') or '').strip()
            if supplier_id:
                s=c.execute("SELECT * FROM suppliers WHERE id=?",(supplier_id,)).fetchone()
                if s:
                    supplier_name=s['name']
            number=(request.form.get('invoice_number') or '').strip()
            try:
                subtotal=float(request.form.get('subtotal') or 0)
                vat=float(request.form.get('vat_amount') or 0)
            except ValueError:
                c.close()
                flash("Montants invalides.")
                return redirect(url_for('purchase_new'))
            total=round(subtotal+vat,2)
            if not supplier_name or not number or subtotal < 0 or vat < 0:
                c.close()
                flash("Fournisseur, numéro et montants valides sont obligatoires.")
                return redirect(url_for('purchase_new'))
            pending_document=(request.form.get('pending_document') or '').strip()
            if pending_document and not re.fullmatch(r'[0-9a-f]{32}\.pdf',pending_document):
                c.close(); abort(400)
            if pending_document and not (_purchase_pdf_dir()/pending_document).is_file():
                c.close(); flash("Le PDF temporaire n'est plus disponible. Réimportez-le.")
                return redirect(url_for('purchase_new'))
            c.execute("""INSERT INTO purchase_invoices(
                         supplier_id,supplier_name,invoice_number,issue_date,due_date,
                         subtotal,vat_amount,total,status,notes,created_at,document_path)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (supplier_id,supplier_name,number,
                       request.form.get('issue_date') or None,
                       request.form.get('due_date') or None,
                       subtotal,vat,total,'unpaid',
                       (request.form.get('notes') or '').strip(),now(),pending_document or None))
            c.commit(); c.close()
            flash("Facture fournisseur enregistrée.")
            return redirect(url_for('purchase_list'))
        c.close()
        return render_template('purchase_new.html',suppliers=suppliers)

    @app.get('/facturation/fournisseurs/<int:supplier_id>')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def supplier_detail(supplier_id):
        c=cx()
        supplier=c.execute("SELECT * FROM suppliers WHERE id=?",(supplier_id,)).fetchone()
        if not supplier:
            c.close(); abort(404)
        purchases=c.execute("SELECT * FROM purchase_invoices WHERE supplier_id=? ORDER BY issue_date DESC,id DESC",(supplier_id,)).fetchall()
        total=sum(float(p['total'] or 0) for p in purchases)
        unpaid=sum(float(p['total'] or 0) for p in purchases if p['status']=='unpaid')
        c.close()
        return render_template('supplier_detail.html',supplier=supplier,purchases=purchases,total=total,unpaid=unpaid)


    @app.get('/facturation/dettes-fournisseurs')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def supplier_debts():
        today = date.today()
        d7 = today + timedelta(days=7)
        d30 = today + timedelta(days=30)
        c = cx()
        rows = c.execute("""
                SELECT id, supplier_id, supplier_name, invoice_number,
                       issue_date, due_date, total, status, created_at
                FROM purchase_invoices
                WHERE COALESCE(status, 'unpaid') != 'paid'
                ORDER BY CASE WHEN due_date IS NULL OR due_date='' THEN 1 ELSE 0 END,
                         due_date ASC, id DESC
            """).fetchall()
        c.close()

        invoices = []
        total_due = overdue = due_7 = due_30 = 0.0
        for row in rows:
            inv = dict(row)
            amount = float(inv.get('total') or 0)
            total_due += amount
            due = None
            if inv.get('due_date'):
                try:
                    due = date.fromisoformat(str(inv['due_date'])[:10])
                except Exception:
                    pass
            inv['is_overdue'] = bool(due and due < today)
            inv['due_in_7'] = bool(due and today <= due <= d7)
            inv['due_in_30'] = bool(due and today <= due <= d30)
            if inv['is_overdue']:
                overdue += amount
            if inv['due_in_7']:
                due_7 += amount
            if inv['due_in_30']:
                due_30 += amount
            invoices.append(inv)

        return render_template(
            'supplier_debts.html',
            invoices=invoices,
            total_due=total_due,
            overdue=overdue,
            due_7=due_7,
            due_30=due_30,
        )


    @app.get('/facturation/previsions-paiements-fournisseurs')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def supplier_payment_forecast():
        today = date.today()
        d7 = today + timedelta(days=7)
        d30 = today + timedelta(days=30)

        c = cx()
        rows = c.execute("""
            SELECT id, supplier_id, supplier_name, invoice_number,
                   issue_date, due_date, total, status, created_at
            FROM purchase_invoices
            WHERE COALESCE(status, 'unpaid') != 'paid'
            ORDER BY
              CASE WHEN due_date IS NULL OR due_date='' THEN 1 ELSE 0 END,
              due_date ASC, id DESC
        """).fetchall()
        c.close()

        overdue = []
        weeks_map = {}
        no_due_date = []
        supplier_map = {}
        total_forecast = 0.0
        due_7 = 0.0
        due_30 = 0.0

        for row in rows:
            inv = dict(row)
            amount = float(inv.get('total') or 0)
            due = None
            if inv.get('due_date'):
                try:
                    due = date.fromisoformat(str(inv['due_date'])[:10])
                except Exception:
                    due = None

            inv['amount'] = amount
            inv['due_date_obj'] = due
            inv['alert'] = None

            supplier_name = (inv.get('supplier_name') or 'Fournisseur sans nom').strip()
            agg = supplier_map.setdefault(
                supplier_name,
                {'supplier_name': supplier_name, 'total': 0.0, 'count': 0}
            )
            agg['total'] += amount
            agg['count'] += 1

            if due is None:
                no_due_date.append(inv)
                continue

            total_forecast += amount

            if due < today:
                inv['alert'] = 'En retard'
                overdue.append(inv)
                continue

            if due <= d7:
                due_7 += amount
                inv['alert'] = 'Échéance proche'
            if due <= d30:
                due_30 += amount

            week_start = due - timedelta(days=due.weekday())
            week_end = week_start + timedelta(days=6)
            key = week_start.isoformat()
            bucket = weeks_map.setdefault(
                key,
                {
                    'week_start': week_start,
                    'week_end': week_end,
                    'total': 0.0,
                    'invoices': [],
                }
            )
            bucket['total'] += amount
            bucket['invoices'].append(inv)

        weeks = [weeks_map[k] for k in sorted(weeks_map)]
        suppliers = sorted(supplier_map.values(), key=lambda x: x['total'], reverse=True)

        return render_template(
            'supplier_payment_forecast.html',
            today=today,
            overdue=overdue,
            weeks=weeks,
            no_due_date=no_due_date,
            suppliers=suppliers,
            total_forecast=total_forecast,
            due_7=due_7,
            due_30=due_30,
        )

    @app.get('/facturation/achats/<int:purchase_id>')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_detail(purchase_id):
        c=cx()
        p=c.execute("SELECT * FROM purchase_invoices WHERE id=?",(purchase_id,)).fetchone()
        if not p:
            c.close(); abort(404)
        supplier=None
        if p['supplier_id']:
            supplier=c.execute("SELECT * FROM suppliers WHERE id=?",(p['supplier_id'],)).fetchone()
        c.close()
        return render_template('purchase_detail.html',p=p,supplier=supplier)

    @app.route('/facturation/achats/<int:purchase_id>/modifier',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_edit(purchase_id):
        c=cx()
        p=c.execute("SELECT * FROM purchase_invoices WHERE id=?",(purchase_id,)).fetchone()
        if not p:
            c.close(); abort(404)
        if p['status']!='unpaid':
            c.close()
            flash("Une facture fournisseur payée est verrouillée.")
            return redirect(url_for('purchase_detail',purchase_id=purchase_id))
        suppliers=c.execute("SELECT * FROM suppliers ORDER BY name ASC").fetchall()
        if request.method=='POST':
            supplier_id=request.form.get('supplier_id') or None
            supplier_name=(request.form.get('supplier_name') or '').strip()
            if supplier_id:
                s=c.execute("SELECT * FROM suppliers WHERE id=?",(supplier_id,)).fetchone()
                if s:
                    supplier_name=s['name']
            number=(request.form.get('invoice_number') or '').strip()
            try:
                subtotal=float(request.form.get('subtotal') or 0)
                vat=float(request.form.get('vat_amount') or 0)
            except ValueError:
                c.close(); flash("Montants invalides.")
                return redirect(url_for('purchase_edit',purchase_id=purchase_id))
            if not supplier_name or not number or subtotal<0 or vat<0:
                c.close(); flash("Fournisseur, numéro et montants valides sont obligatoires.")
                return redirect(url_for('purchase_edit',purchase_id=purchase_id))
            c.execute("""UPDATE purchase_invoices SET supplier_id=?,supplier_name=?,invoice_number=?,
                         issue_date=?,due_date=?,subtotal=?,vat_amount=?,total=?,notes=? WHERE id=?""",
                      (supplier_id,supplier_name,number,request.form.get('issue_date') or None,
                       request.form.get('due_date') or None,subtotal,vat,round(subtotal+vat,2),
                       (request.form.get('notes') or '').strip(),purchase_id))
            c.commit(); c.close()
            flash("Facture fournisseur mise à jour.")
            return redirect(url_for('purchase_detail',purchase_id=purchase_id))
        c.close()
        return render_template('purchase_edit.html',p=p,suppliers=suppliers)

    @app.post('/facturation/achats/<int:purchase_id>/justificatif')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_document_upload(purchase_id):
        c=cx()
        p=c.execute("SELECT * FROM purchase_invoices WHERE id=?",(purchase_id,)).fetchone()
        if not p:
            c.close(); abort(404)
        try:
            stored=_save_purchase_pdf(request.files.get('document'))
        except ValueError as e:
            c.close()
            flash(str(e))
            return redirect(url_for('purchase_detail',purchase_id=purchase_id))
        if not stored:
            c.close()
            flash("Sélectionnez un fichier PDF.")
            return redirect(url_for('purchase_detail',purchase_id=purchase_id))

        old=p['document_path']
        c.execute("UPDATE purchase_invoices SET document_path=? WHERE id=?",(stored,purchase_id))
        c.commit(); c.close()

        if old and old != stored:
            try:
                (_purchase_pdf_dir() / old).unlink(missing_ok=True)
            except OSError:
                pass
        flash("Justificatif PDF enregistré.")
        return redirect(url_for('purchase_detail',purchase_id=purchase_id))

    @app.get('/facturation/achats/<int:purchase_id>/justificatif')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_document_view(purchase_id):
        c=cx()
        p=c.execute("SELECT * FROM purchase_invoices WHERE id=?",(purchase_id,)).fetchone()
        c.close()
        if not p or not p['document_path']:
            abort(404)

        # document_path contains only a generated UUID filename, never a user path.
        path=_purchase_pdf_dir() / p['document_path']
        if not path.is_file():
            abort(404)
        data=path.read_bytes()
        response=Response(data,mimetype='application/pdf')
        safe_number=re.sub(r'[^A-Za-z0-9._-]+','-',p['invoice_number'] or 'facture')
        response.headers['Content-Disposition']=f'inline; filename="justificatif-{safe_number}.pdf"'
        response.headers['X-Content-Type-Options']='nosniff'
        return response

    @app.post('/facturation/achats/<int:purchase_id>/payer')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def purchase_mark_paid(purchase_id):
        c=cx()
        p=c.execute("SELECT * FROM purchase_invoices WHERE id=?",(purchase_id,)).fetchone()
        if not p:
            c.close()
            abort(404)
        if p['status']=='unpaid':
            c.execute("UPDATE purchase_invoices SET status='paid',paid_at=? WHERE id=?",(now(),purchase_id))
            c.commit()
            flash("Facture fournisseur marquée comme payée.")
        c.close()
        return redirect(url_for('purchase_list'))

    @app.route('/facturation')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def invoicing_list():
        c=cx()
        rows=c.execute('SELECT * FROM outgoing_invoices ORDER BY id DESC').fetchall()
        c.close()
        totals={'draft':0,'sent':0,'overdue':0,'paid':0,'cancelled':0}
        display_statuses={}
        for r in rows:
            status=_display_status(r)
            display_statuses[r['id']]=status
            if status in totals: totals[status]+=r['total'] or 0
        return render_template('invoicing_list.html',rows=rows,totals=totals,display_statuses=display_statuses)

    @app.route('/facturation/nouvelle',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_new():
        c=cx()
        company_row=c.execute('SELECT * FROM company WHERE id=1').fetchone()
        if not company_row or not company_row['name']:
            c.close()
            flash("Complète d'abord ton profil entreprise (nom, adresse, SIRET) avant de créer une facture.")
            return redirect(url_for('company'))

        if request.method=='POST':
            client_name=request.form.get('client_name','').strip()
            client_address=request.form.get('client_address','').strip()
            client_email=request.form.get('client_email','').strip()
            due_date=request.form.get('due_date','').strip()
            notes=request.form.get('notes','').strip()
            items=_compute_line_items(request.form)

            if not client_name or not items:
                c.close()
                flash('Nom du client et au moins une ligne de facture requis.')
                return redirect(url_for('invoicing_new'))

            subtotal,vat_amount,total=_totals(items)
            invoice_number=_next_invoice_number(c)
            issue_date=date.today().isoformat()
            token=secrets.token_urlsafe(20)

            c.execute('''INSERT INTO outgoing_invoices(invoice_number,client_name,client_address,client_email,issue_date,due_date,
                         line_items,subtotal,vat_amount,total,notes,status,public_token,created_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,'draft',?,?)''',
                (invoice_number,client_name,client_address,client_email,issue_date,due_date or None,
                 json.dumps(items,ensure_ascii=False),subtotal,vat_amount,total,notes,token,now()))
            c.commit()
            new_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.close()
            ac=auth_cx()
            ac.execute('INSERT INTO outgoing_invoice_tokens(token,organization_id,invoice_local_id,created_at) VALUES(?,?,?,?)',
                (token,session['org_id'],new_id,now())); ac.commit(); ac.close()
            log_activity('INVOICE_CREATED',f'Facture {invoice_number} créée ({total:,.0f} € TTC)')
            flash(f'Facture {invoice_number} créée en brouillon.')
            return redirect(url_for('invoicing_detail',invoice_id=new_id))

        clients=c.execute("SELECT * FROM invoicing_clients ORDER BY lower(name)").fetchall()
        c.close()
        return render_template('invoicing_new.html',company=company_row,today=date.today().isoformat(),clients=clients)

    @app.route('/facturation/<int:invoice_id>')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def invoicing_detail(invoice_id):
        c=cx()
        inv=c.execute('SELECT * FROM outgoing_invoices WHERE id=?',(invoice_id,)).fetchone()
        c.close()
        if not inv: abort(404)
        items=json.loads(inv['line_items'] or '[]')
        c=cx()
        credits=c.execute("SELECT * FROM outgoing_credit_notes WHERE original_invoice_id=? ORDER BY id DESC",(invoice_id,)).fetchall()
        credited_total=_credited_total(c,invoice_id)
        reminders=c.execute("SELECT * FROM invoice_reminders WHERE invoice_id=? ORDER BY reminder_number DESC",(invoice_id,)).fetchall()
        c.close()
        return render_template('invoicing_detail.html',inv=inv,items=items,display_status=_display_status(inv),
                               credits=credits,credited_total=credited_total,reminders=reminders,
                               creditable_total=max(0,float(inv['total'] or 0)-credited_total))

    @app.route('/facturation/<int:invoice_id>/pdf')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def invoicing_pdf(invoice_id):
        c=cx()
        inv=c.execute('SELECT * FROM outgoing_invoices WHERE id=?',(invoice_id,)).fetchone()
        company_row=c.execute('SELECT * FROM company WHERE id=1').fetchone()
        c.close()
        if not inv: abort(404)
        pdf_bytes=_render_invoice_pdf(inv,company_row)
        if pdf_bytes is None:
            flash("La génération PDF nécessite le paquet 'fpdf2' — lance : pip install -r requirements.txt")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        return Response(pdf_bytes,mimetype='application/pdf',
            headers={'Content-Disposition':f'attachment; filename="{inv["invoice_number"]}.pdf"'})

    @app.route('/facturation/<int:invoice_id>/envoyer',methods=['POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_send(invoice_id):
        c=cx()
        inv=c.execute('SELECT * FROM outgoing_invoices WHERE id=?',(invoice_id,)).fetchone()
        if not inv:
            c.close(); abort(404)
        if inv['status'] in ('paid','cancelled'):
            c.close()
            flash("Cette facture ne peut plus être envoyée.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        if not inv['client_email']:
            c.close()
            flash("Aucun email client renseigné pour cette facture.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))

        org=current_org()
        base=os.environ.get('APP_BASE_URL',request.host_url.rstrip('/'))
        link=f"{base}{url_for('public_invoice_view',token=inv['public_token'])}"
        html=render_template('email_transactional.html',title=f"Facture {inv['invoice_number']} — {org['name']}",
            intro=f"Voici votre facture {inv['invoice_number']} de {org['name']}, d'un montant de {inv['total']:,.2f} € TTC.",
            cta_label='Consulter la facture',cta_url=link,footer='')
        result=send_email(inv['client_email'],f"Facture {inv['invoice_number']} — {org['name']}",html)

        if result.get('dry_run'):
            flash(f"Service email non configuré — facture non envoyée réellement (mode simulation) à {inv['client_email']}.")
        elif result.get('sent'):
            issue_date = date.today().isoformat() if inv['status']=='draft' else inv['issue_date']
            c.execute("UPDATE outgoing_invoices SET status='sent',sent_at=?,issue_date=? WHERE id=?",
                      (now(),issue_date,invoice_id))
            c.commit()
            log_activity('INVOICE_SENT',f"Facture {inv['invoice_number']} envoyée à {inv['client_email']}")
            flash(f"Facture envoyée à {inv['client_email']}.")
        else:
            flash(f"Échec de l'envoi : {result.get('error','erreur inconnue')}")
        c.close()
        return redirect(url_for('invoicing_detail',invoice_id=invoice_id))

    @app.route('/facturation/<int:invoice_id>/relancer',methods=['POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_remind(invoice_id):
        c=cx()
        inv=c.execute('SELECT * FROM outgoing_invoices WHERE id=?',(invoice_id,)).fetchone()
        if not inv:
            c.close(); abort(404)
        if inv['status']!='sent' or _display_status(inv)!='overdue':
            c.close()
            flash("Seule une facture envoyée et échue peut être relancée.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        if not inv['client_email']:
            c.close()
            flash("Aucun email client renseigné pour cette facture.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        if _credited_total(c,invoice_id) >= float(inv['total'] or 0)-0.01:
            c.close()
            flash("Cette facture est entièrement couverte par un avoir et ne peut pas être relancée.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))

        row=c.execute("SELECT COUNT(*) AS n FROM invoice_reminders WHERE invoice_id=?",(invoice_id,)).fetchone()
        reminder_number=int(row['n'] or 0)+1
        org=current_org()
        base=os.environ.get('APP_BASE_URL',request.host_url.rstrip('/'))
        link=f"{base}{url_for('public_invoice_view',token=inv['public_token'])}"
        html=render_template(
            'email_transactional.html',
            title=f"Relance facture {inv['invoice_number']} — {org['name']}",
            intro=(f"Sauf erreur de notre part, la facture {inv['invoice_number']} "
                   f"d'un montant de {inv['total']:,.2f} € TTC, échue le {inv['due_date']}, "
                   f"reste impayée. Si votre règlement a déjà été effectué, merci de ne pas tenir compte de cette relance."),
            cta_label='Consulter la facture',cta_url=link,footer=''
        )
        result=send_email(inv['client_email'],f"Relance facture {inv['invoice_number']} — {org['name']}",html)
        if result.get('dry_run'):
            flash(f"Service email non configuré — relance non envoyée réellement (mode simulation) à {inv['client_email']}.")
        elif result.get('sent'):
            sent_at=now()
            c.execute("INSERT INTO invoice_reminders(invoice_id,recipient_email,sent_at,reminder_number) VALUES(?,?,?,?)",
                      (invoice_id,inv['client_email'],sent_at,reminder_number))
            c.commit()
            log_activity('INVOICE_REMINDER_SENT',f"Relance n°{reminder_number} pour {inv['invoice_number']} envoyée à {inv['client_email']}")
            flash(f"Relance n°{reminder_number} envoyée à {inv['client_email']}.")
        else:
            flash(f"Échec de la relance : {result.get('error','erreur inconnue')}")
        c.close()
        return redirect(url_for('invoicing_detail',invoice_id=invoice_id))

    @app.route('/facturation/<int:invoice_id>/marquer-payee',methods=['POST'])
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def invoicing_mark_paid(invoice_id):
        c=cx()
        inv=c.execute('SELECT * FROM outgoing_invoices WHERE id=?',(invoice_id,)).fetchone()
        if not inv:
            c.close(); abort(404)
        if inv['status']=='cancelled':
            c.close()
            flash("Une facture annulée ne peut pas être marquée comme payée.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        c.execute("UPDATE outgoing_invoices SET status='paid',paid_at=? WHERE id=?",(now(),invoice_id))
        c.commit(); c.close()
        log_activity('INVOICE_PAID',f"Facture {inv['invoice_number']} marquée payée")
        flash(f"Facture {inv['invoice_number']} marquée comme payée.")
        return redirect(url_for('invoicing_detail',invoice_id=invoice_id))


    @app.route('/facturation/<int:invoice_id>/modifier',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_edit(invoice_id):
        c=cx()
        inv=c.execute('SELECT * FROM outgoing_invoices WHERE id=?',(invoice_id,)).fetchone()
        if not inv:
            c.close(); abort(404)
        if inv['status']!='draft':
            c.close()
            flash("Seule une facture en brouillon peut être modifiée.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        if request.method=='POST':
            client_name=request.form.get('client_name','').strip()
            client_address=request.form.get('client_address','').strip()
            client_email=request.form.get('client_email','').strip()
            due_date=request.form.get('due_date','').strip()
            notes=request.form.get('notes','').strip()
            items=_compute_line_items(request.form)
            if not client_name or not items:
                c.close()
                flash('Nom du client et au moins une ligne de facture requis.')
                return redirect(url_for('invoicing_edit',invoice_id=invoice_id))
            subtotal,vat_amount,total=_totals(items)
            c.execute("UPDATE outgoing_invoices SET client_name=?,client_address=?,client_email=?,due_date=?,line_items=?,subtotal=?,vat_amount=?,total=?,notes=? WHERE id=? AND status='draft'",
                (client_name,client_address,client_email,due_date or None,json.dumps(items,ensure_ascii=False),subtotal,vat_amount,total,notes,invoice_id))
            c.commit(); c.close()
            log_activity('INVOICE_UPDATED',f"Facture {inv['invoice_number']} modifiée")
            flash(f"Facture {inv['invoice_number']} mise à jour.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        items=json.loads(inv['line_items'] or '[]')
        c.close()
        return render_template('invoicing_edit.html',inv=inv,items=items)

    @app.route('/facturation/<int:invoice_id>/annuler',methods=['POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_cancel(invoice_id):
        c=cx()
        inv=c.execute('SELECT * FROM outgoing_invoices WHERE id=?',(invoice_id,)).fetchone()
        if not inv:
            c.close(); abort(404)
        if inv['status'] in ('sent','paid'):
            c.close()
            flash("Une facture émise ne peut plus être annulée directement.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        if inv['status']=='cancelled':
            c.close()
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        c.execute("UPDATE outgoing_invoices SET status='cancelled' WHERE id=?",(invoice_id,))
        c.commit(); c.close()
        log_activity('INVOICE_CANCELLED',f"Facture {inv['invoice_number']} annulée")
        flash(f"Facture {inv['invoice_number']} annulée.")
        return redirect(url_for('invoicing_detail',invoice_id=invoice_id))

    @app.route('/facturation/<int:invoice_id>/avoir',methods=['GET','POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_credit_new(invoice_id):
        c=cx()
        inv=c.execute('SELECT * FROM outgoing_invoices WHERE id=?',(invoice_id,)).fetchone()
        if not inv:
            c.close(); abort(404)
        if inv['status'] not in ('sent','paid'):
            c.close()
            flash("Un avoir ne peut être créé que pour une facture émise.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))
        already=_credited_total(c,invoice_id)
        remaining=max(0,float(inv['total'] or 0)-already)
        if remaining <= 0.005:
            c.close()
            flash("Cette facture est déjà intégralement créditée.")
            return redirect(url_for('invoicing_detail',invoice_id=invoice_id))

        if request.method=='POST':
            reason=request.form.get('reason','').strip()
            try:
                amount_ttc=float(request.form.get('amount_ttc','0').replace(',','.'))
            except ValueError:
                amount_ttc=0
            if not reason or amount_ttc <= 0 or amount_ttc > remaining + 0.005:
                c.close()
                flash(f"Motif requis et montant TTC compris entre 0,01 € et {remaining:.2f} €.")
                return redirect(url_for('invoicing_credit_new',invoice_id=invoice_id))

            ratio=amount_ttc/float(inv['total'])
            source_items=json.loads(inv['line_items'] or '[]')
            items=[]
            for it in source_items:
                line_total=round(float(it['line_total'])*ratio,2)
                items.append({'label':f"Avoir — {it['label']}",'qty':1.0,
                              'unit_price':line_total,'vat_rate':float(it['vat_rate']),
                              'line_total':line_total})
            subtotal=round(float(inv['subtotal'])*ratio,2)
            vat_amount=round(amount_ttc-subtotal,2)
            credit_number=_next_credit_number(c)
            c.execute("""INSERT INTO outgoing_credit_notes
                (credit_number,original_invoice_id,original_invoice_number,client_name,issue_date,
                 line_items,subtotal,vat_amount,total,reason,status,created_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,'issued',?)""",
                (credit_number,invoice_id,inv['invoice_number'],inv['client_name'],date.today().isoformat(),
                 json.dumps(items,ensure_ascii=False),subtotal,vat_amount,round(amount_ttc,2),reason,now()))
            c.commit()
            credit_id=c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.close()
            log_activity('CREDIT_NOTE_CREATED',f"Avoir {credit_number} créé pour {inv['invoice_number']} ({amount_ttc:.2f} € TTC)")
            flash(f"Avoir {credit_number} créé.")
            return redirect(url_for('invoicing_credit_detail',credit_id=credit_id))

        c.close()
        return render_template('invoicing_credit_new.html',inv=inv,remaining=remaining)

    @app.route('/facturation/avoir/<int:credit_id>')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def invoicing_credit_detail(credit_id):
        c=cx()
        credit=c.execute('SELECT * FROM outgoing_credit_notes WHERE id=?',(credit_id,)).fetchone()
        c.close()
        if not credit: abort(404)
        items=json.loads(credit['line_items'] or '[]')
        return render_template('invoicing_credit_detail.html',credit=credit,items=items)

    @app.route('/facturation/avoir/<int:credit_id>/pdf')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def invoicing_credit_pdf(credit_id):
        c=cx()
        credit=c.execute('SELECT * FROM outgoing_credit_notes WHERE id=?',(credit_id,)).fetchone()
        company_row=c.execute('SELECT * FROM company WHERE id=1').fetchone()
        c.close()
        if not credit: abort(404)
        pdf_bytes=_render_credit_pdf(credit,company_row)
        if pdf_bytes is None:
            flash("La génération PDF nécessite le paquet 'fpdf2'.")
            return redirect(url_for('invoicing_credit_detail',credit_id=credit_id))
        return Response(pdf_bytes,mimetype='application/pdf',
            headers={'Content-Disposition':f'attachment; filename="{credit["credit_number"]}.pdf"'})

    @app.route('/devis/<token>')
    def public_quote_view(token):
        ac=auth_cx()
        mapping=ac.execute('SELECT * FROM outgoing_quote_tokens WHERE token=?',(token,)).fetchone()
        ac.close()
        if not mapping: abort(404)
        tc=tenant_cx_direct(mapping['organization_id'])
        q=tc.execute('SELECT * FROM outgoing_quotes WHERE id=?',(mapping['quote_local_id'],)).fetchone()
        company_row=tc.execute('SELECT * FROM company WHERE id=1').fetchone() if q else None
        tc.close()
        if not q: abort(404)
        return render_template('invoicing_quote_public.html',q=q,
                               items=json.loads(q['line_items'] or '[]'),
                               company=company_row,token=token,
                               status_label=_quote_status_label(q['status']))

    @app.post('/devis/<token>/accepter')
    def public_quote_accept(token):
        ac=auth_cx()
        mapping=ac.execute('SELECT * FROM outgoing_quote_tokens WHERE token=?',(token,)).fetchone()
        ac.close()
        if not mapping: abort(404)
        tc=tenant_cx_direct(mapping['organization_id'])
        q=tc.execute('SELECT * FROM outgoing_quotes WHERE id=?',(mapping['quote_local_id'],)).fetchone()
        if not q:
            tc.close(); abort(404)
        if q['status']=='sent':
            tc.execute("UPDATE outgoing_quotes SET status='accepted',accepted_at=? WHERE id=?",
                       (now(),mapping['quote_local_id']))
            tc.commit()
        tc.close()
        return redirect(url_for('public_quote_view',token=token))

    @app.post('/devis/<token>/refuser')
    def public_quote_refuse(token):
        ac=auth_cx()
        mapping=ac.execute('SELECT * FROM outgoing_quote_tokens WHERE token=?',(token,)).fetchone()
        ac.close()
        if not mapping: abort(404)
        tc=tenant_cx_direct(mapping['organization_id'])
        q=tc.execute('SELECT * FROM outgoing_quotes WHERE id=?',(mapping['quote_local_id'],)).fetchone()
        if not q:
            tc.close(); abort(404)
        if q['status']=='sent':
            tc.execute("UPDATE outgoing_quotes SET status='refused',refused_at=? WHERE id=?",
                       (now(),mapping['quote_local_id']))
            tc.commit()
        tc.close()
        return redirect(url_for('public_quote_view',token=token))

    @app.route('/devis/<token>/pdf')
    def public_quote_pdf(token):
        ac=auth_cx()
        mapping=ac.execute('SELECT * FROM outgoing_quote_tokens WHERE token=?',(token,)).fetchone()
        ac.close()
        if not mapping: abort(404)
        tc=tenant_cx_direct(mapping['organization_id'])
        q=tc.execute('SELECT * FROM outgoing_quotes WHERE id=?',(mapping['quote_local_id'],)).fetchone()
        company_row=tc.execute('SELECT * FROM company WHERE id=1').fetchone() if q else None
        tc.close()
        if not q: abort(404)
        pdf_bytes=_render_quote_pdf(q,company_row)
        if pdf_bytes is None: abort(404)
        return Response(pdf_bytes,mimetype='application/pdf',
            headers={'Content-Disposition':f'inline; filename="{q["quote_number"]}.pdf"'})

    @app.route('/facture/<token>')
    def public_invoice_view(token):
        """Vue publique d'une facture émise — aucune authentification requise.
        Résolution par token via la table auth partagée (pas de dépendance à une
        session — un client externe n'en a pas), sans exposer la structure interne."""
        ac=auth_cx()
        mapping=ac.execute('SELECT * FROM outgoing_invoice_tokens WHERE token=?',(token,)).fetchone()
        ac.close()
        if not mapping: abort(404)
        tc=tenant_cx_direct(mapping['organization_id'])
        inv=tc.execute('SELECT * FROM outgoing_invoices WHERE id=?',(mapping['invoice_local_id'],)).fetchone()
        company_row=tc.execute('SELECT * FROM company WHERE id=1').fetchone() if inv else None
        tc.close()
        if not inv: abort(404)
        items=json.loads(inv['line_items'] or '[]')
        return render_template('invoicing_public.html',inv=inv,items=items,company=company_row,token=token)

    @app.route('/facture/<token>/pdf')
    def public_invoice_pdf(token):
        ac=auth_cx()
        mapping=ac.execute('SELECT * FROM outgoing_invoice_tokens WHERE token=?',(token,)).fetchone()
        ac.close()
        if not mapping: abort(404)
        tc=tenant_cx_direct(mapping['organization_id'])
        inv=tc.execute('SELECT * FROM outgoing_invoices WHERE id=?',(mapping['invoice_local_id'],)).fetchone()
        company_row=tc.execute('SELECT * FROM company WHERE id=1').fetchone() if inv else None
        tc.close()
        if not inv: abort(404)
        pdf_bytes=_render_invoice_pdf(inv,company_row)
        if pdf_bytes is None: abort(404)
        return Response(pdf_bytes,mimetype='application/pdf',
            headers={'Content-Disposition':f'inline; filename="{inv["invoice_number"]}.pdf"'})


def _render_invoice_pdf(inv,company_row):
    """Génère le PDF d'une facture émise. Retourne None si fpdf2 n'est pas installé
    (dégradation propre, jamais d'erreur 500)."""
    try:
        from fpdf import FPDF
    except ImportError:
        return None

    def safe(text):
        if text is None: return ''
        text=str(text)
        repl={'—':'-','–':'-','\u2018':"'",'\u2019':"'",'\u201c':'"','\u201d':'"','…':'...','\xa0':' ','€':'EUR'}
        for a,b in repl.items(): text=text.replace(a,b)
        return text.encode('latin-1',errors='replace').decode('latin-1')

    items=json.loads(inv['line_items'] or '[]')
    pdf=FPDF(orientation='P',unit='mm',format='A4')
    pdf.set_auto_page_break(auto=True,margin=18)
    pdf.add_page()

    pdf.set_font('Helvetica','B',20); pdf.set_text_color(17,24,39)
    pdf.cell(0,12,safe(f"Facture {inv['invoice_number']}"),ln=1)
    pdf.set_font('Helvetica','',11); pdf.set_text_color(107,114,128)
    if company_row:
        pdf.cell(0,6,safe(company_row['name'] or ''),ln=1)
        if company_row['address']: pdf.cell(0,6,safe(company_row['address']),ln=1)
        if company_row['siret']: pdf.cell(0,6,safe(f"SIRET : {company_row['siret']}"),ln=1)
        if company_row['vat_number']: pdf.cell(0,6,safe(f"TVA : {company_row['vat_number']}"),ln=1)
    pdf.ln(6)

    pdf.set_text_color(17,24,39); pdf.set_font('Helvetica','B',12)
    pdf.cell(0,7,'Facturé à :',ln=1)
    pdf.set_font('Helvetica','',11)
    pdf.cell(0,6,safe(inv['client_name']),ln=1)
    if inv['client_address']: pdf.cell(0,6,safe(inv['client_address']),ln=1)
    pdf.ln(4)
    pdf.set_font('Helvetica','',10); pdf.set_text_color(107,114,128)
    pdf.cell(0,6,safe(f"Date d'émission : {inv['issue_date']}"),ln=1)
    if inv['due_date']: pdf.cell(0,6,safe(f"Échéance : {inv['due_date']}"),ln=1)
    pdf.ln(8)

    pdf.set_fill_color(243,244,246); pdf.set_text_color(17,24,39); pdf.set_font('Helvetica','B',10)
    pdf.cell(80,8,'Description',border=0,fill=True)
    pdf.cell(20,8,'Qté',border=0,fill=True,align='R')
    pdf.cell(30,8,'Prix unit.',border=0,fill=True,align='R')
    pdf.cell(20,8,'TVA',border=0,fill=True,align='R')
    pdf.cell(30,8,'Total HT',border=0,fill=True,align='R',ln=1)
    pdf.set_font('Helvetica','',10)
    for it in items:
        pdf.cell(80,7,safe(it['label']))
        pdf.cell(20,7,safe(f"{it['qty']:g}"),align='R')
        pdf.cell(30,7,safe(f"{it['unit_price']:,.2f} EUR"),align='R')
        pdf.cell(20,7,safe(f"{it['vat_rate']:g}%"),align='R')
        pdf.cell(30,7,safe(f"{it['line_total']:,.2f} EUR"),align='R',ln=1)
    pdf.ln(6)

    pdf.set_font('Helvetica','',11)
    pdf.cell(150,7,'Sous-total HT',align='R')
    pdf.cell(30,7,safe(f"{inv['subtotal']:,.2f} EUR"),align='R',ln=1)
    pdf.cell(150,7,'TVA',align='R')
    pdf.cell(30,7,safe(f"{inv['vat_amount']:,.2f} EUR"),align='R',ln=1)
    pdf.set_font('Helvetica','B',13)
    pdf.cell(150,9,'Total TTC',align='R')
    pdf.cell(30,9,safe(f"{inv['total']:,.2f} EUR"),align='R',ln=1)

    if inv['notes']:
        pdf.ln(8); pdf.set_font('Helvetica','',9); pdf.set_text_color(107,114,128)
        pdf.multi_cell(0,5,safe(inv['notes']))

    pdf.ln(10); pdf.set_font('Helvetica','I',8); pdf.set_text_color(150,150,150)
    pdf.multi_cell(0,4,safe("Document genere via ProfitOS. Ce document n'est pas emis via une Plateforme Agreee DGFiP au sens de la reforme de facturation electronique."))

    return bytes(pdf.output(dest='S'))


def _render_credit_pdf(credit,company_row):
    try:
        from fpdf import FPDF
    except ImportError:
        return None
    def safe(text):
        if text is None: return ''
        text=str(text)
        repl={'—':'-','–':'-','€':'EUR','\xa0':' '}
        for a,b in repl.items(): text=text.replace(a,b)
        return text.encode('latin-1',errors='replace').decode('latin-1')
    items=json.loads(credit['line_items'] or '[]')
    pdf=FPDF(orientation='P',unit='mm',format='A4'); pdf.add_page()
    pdf.set_font('Helvetica','B',20)
    pdf.cell(0,12,safe(f"Avoir {credit['credit_number']}"),ln=1)
    pdf.set_font('Helvetica','',10)
    if company_row:
        pdf.cell(0,6,safe(company_row['name'] or ''),ln=1)
        if company_row['siret']: pdf.cell(0,6,safe(f"SIRET : {company_row['siret']}"),ln=1)
    pdf.ln(5)
    pdf.set_font('Helvetica','B',11)
    pdf.cell(0,7,safe(f"Facture d'origine : {credit['original_invoice_number']}"),ln=1)
    pdf.set_font('Helvetica','',10)
    pdf.cell(0,6,safe(f"Client : {credit['client_name']}"),ln=1)
    pdf.cell(0,6,safe(f"Date : {credit['issue_date']}"),ln=1)
    pdf.multi_cell(0,6,safe(f"Motif : {credit['reason']}"))
    pdf.ln(5)
    for it in items:
        pdf.cell(130,7,safe(it['label']))
        pdf.cell(50,7,safe(f"-{it['line_total']:,.2f} EUR"),align='R',ln=1)
    pdf.ln(5); pdf.set_font('Helvetica','',11)
    pdf.cell(140,7,'Sous-total HT',align='R'); pdf.cell(40,7,safe(f"-{credit['subtotal']:,.2f} EUR"),align='R',ln=1)
    pdf.cell(140,7,'TVA',align='R'); pdf.cell(40,7,safe(f"-{credit['vat_amount']:,.2f} EUR"),align='R',ln=1)
    pdf.set_font('Helvetica','B',13)
    pdf.cell(140,9,'Total TTC avoir',align='R'); pdf.cell(40,9,safe(f"-{credit['total']:,.2f} EUR"),align='R',ln=1)
    return bytes(pdf.output(dest='S'))


def _render_quote_pdf(q,company_row):
    try:
        from fpdf import FPDF
    except ImportError:
        return None
    def safe(text):
        if text is None: return ''
        text=str(text)
        for a,b in {'—':'-','–':'-','€':'EUR','\xa0':' '}.items(): text=text.replace(a,b)
        return text.encode('latin-1',errors='replace').decode('latin-1')
    items=json.loads(q['line_items'] or '[]')
    pdf=FPDF(orientation='P',unit='mm',format='A4'); pdf.add_page()
    pdf.set_font('Helvetica','B',20); pdf.cell(0,12,safe(f"Devis {q['quote_number']}"),ln=1)
    pdf.set_font('Helvetica','',10)
    if company_row:
        pdf.cell(0,6,safe(company_row['name'] or ''),ln=1)
        if company_row['siret']: pdf.cell(0,6,safe(f"SIRET : {company_row['siret']}"),ln=1)
    pdf.ln(4); pdf.cell(0,6,safe(f"Client : {q['client_name']}"),ln=1)
    if q['client_address']: pdf.multi_cell(0,6,safe(q['client_address']))
    pdf.cell(0,6,safe(f"Date : {q['issue_date']}"),ln=1)
    if q['valid_until']: pdf.cell(0,6,safe(f"Valable jusqu'au : {q['valid_until']}"),ln=1)
    pdf.ln(5)
    for it in items:
        pdf.cell(130,7,safe(f"{it['label']} ({it['qty']} x {it['unit_price']:.2f} EUR, TVA {it['vat_rate']:.1f}%)"))
        pdf.cell(50,7,safe(f"{it['line_total']:,.2f} EUR"),align='R',ln=1)
    pdf.ln(5)
    pdf.cell(140,7,'Sous-total HT',align='R'); pdf.cell(40,7,safe(f"{q['subtotal']:,.2f} EUR"),align='R',ln=1)
    pdf.cell(140,7,'TVA',align='R'); pdf.cell(40,7,safe(f"{q['vat_amount']:,.2f} EUR"),align='R',ln=1)
    pdf.set_font('Helvetica','B',13)
    pdf.cell(140,9,'Total TTC',align='R'); pdf.cell(40,9,safe(f"{q['total']:,.2f} EUR"),align='R',ln=1)
    if q['notes']:
        pdf.ln(5); pdf.set_font('Helvetica','',10); pdf.multi_cell(0,6,safe(q['notes']))
    return bytes(pdf.output(dest='S'))

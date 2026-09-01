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
            items=_parse_items(request.form)
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
        c=cx(); q=c.execute('SELECT * FROM outgoing_quotes WHERE id=?',(quote_id,)).fetchone()
        if not q: c.close(); abort(404)
        if q['status']!='draft':
            c.close(); flash("Seul un devis brouillon peut être envoyé.")
            return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))
        c.execute("UPDATE outgoing_quotes SET status='sent',sent_at=? WHERE id=?",(now(),quote_id))
        c.commit(); c.close()
        log_activity('QUOTE_SENT',f"Devis {q['quote_number']} marqué envoyé")
        flash(f"Devis {q['quote_number']} marqué comme envoyé.")
        return redirect(url_for('invoicing_quote_detail',quote_id=quote_id))

    @app.post('/facturation/devis/<int:quote_id>/accepter')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_quote_accept(quote_id):
        c=cx(); q=c.execute('SELECT * FROM outgoing_quotes WHERE id=?',(quote_id,)).fetchone()
        if not q: c.close(); abort(404)
        if q['status']!='sent':
            c.close(); flash("Seul un devis envoyé peut être accepté.")
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
        c=cx(); q=c.execute('SELECT * FROM outgoing_quotes WHERE id=?',(quote_id,)).fetchone()
        if not q: c.close(); abort(404)
        if q['status'] not in ('sent','accepted'):
            c.close(); flash("Ce devis ne peut pas être refusé dans son état actuel.")
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
        invoice_number=_next_number(c)
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
        c.close()
        return render_template('invoicing_detail.html',inv=inv,items=items,display_status=_display_status(inv),
                               credits=credits,credited_total=credited_total,
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

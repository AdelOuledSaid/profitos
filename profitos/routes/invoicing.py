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


def register(app):
    @app.route('/facturation')
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def invoicing_list():
        c=cx()
        rows=c.execute('SELECT * FROM outgoing_invoices ORDER BY id DESC').fetchall()
        c.close()
        totals={'draft':0,'sent':0,'paid':0}
        for r in rows:
            if r['status'] in totals: totals[r['status']]+=r['total'] or 0
        return render_template('invoicing_list.html',rows=rows,totals=totals)

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
            count=c.execute('SELECT COUNT(*) n FROM outgoing_invoices').fetchone()['n']
            invoice_number=f"FA-{datetime.now(timezone.utc).year}-{count+1:03d}"
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

        c.close()
        return render_template('invoicing_new.html',company=company_row,today=date.today().isoformat())

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
        return render_template('invoicing_detail.html',inv=inv,items=items)

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
            c.execute("UPDATE outgoing_invoices SET status='sent',sent_at=? WHERE id=?",(now(),invoice_id))
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
        c.execute("UPDATE outgoing_invoices SET status='paid',paid_at=? WHERE id=?",(now(),invoice_id))
        c.commit(); c.close()
        log_activity('INVOICE_PAID',f"Facture {inv['invoice_number']} marquée payée")
        flash(f"Facture {inv['invoice_number']} marquée comme payée.")
        return redirect(url_for('invoicing_detail',invoice_id=invoice_id))

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

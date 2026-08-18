from profitos.runtime import *



def register(app):
    @app.route('/actions')
    @login_required
    @require_area('actions')
    def actions():
        c=cx(); rows=c.execute("SELECT * FROM actions ORDER BY CASE status WHEN 'PENDING' THEN 0 WHEN 'APPROVED' THEN 1 ELSE 2 END,id DESC").fetchall(); c.close(); return render_template('actions.html',rows=rows)

    @app.route('/actions/create/<kind>/<int:item_id>',methods=['POST'])
    @login_required
    def create_action(kind,item_id):
        kind=kind.upper()
        if not can_access(KIND_TO_AREA.get(kind,'')):
            flash("Votre rôle ne donne pas accès à cette section."); return redirect(url_for('actions'))
        c=cx()
        if kind=='RECOVER':
            o=c.execute('SELECT *,MAX(amount-paid_amount,0) outstanding FROM invoices WHERE id=?',(item_id,)).fetchone()
            if o['kind']=='RETENTION':
                title=f"Demander la levée de retenue — {o['customer']} — #{o['invoice_number']}"
                draft=f"Objet : Demande de levée de la retenue de garantie — Facture {o['invoice_number']}\n\nBonjour,\n\nLa retenue de garantie de {o['outstanding']:,.2f} € appliquée sur la facture {o['invoice_number']} est libérable{' depuis le ' + o['retention_release_date'] if o['retention_release_date'] else ''}. Pouvez-vous nous confirmer la date de virement ?\n\nCordialement"
            else:
                title=f"Relancer {o['customer']} — #{o['invoice_number']}"
                draft=f"Objet : Relance facture {o['invoice_number']}\n\nBonjour,\n\nLa facture {o['invoice_number']} présente un solde de {o['outstanding']:,.2f} € arrivé à échéance depuis {o['days_overdue']} jours. Pouvez-vous nous confirmer sa date de règlement ?\n\nCordialement"
            expected=o['outstanding']
        else:
            o=c.execute('SELECT * FROM opportunities WHERE id=? AND type=?',(item_id,kind)).fetchone()
            if kind=='SAVE':title=f"Vérifier — {o['title']}"; draft=f"Signal SAVE : {o['title']}\nValeur potentielle : {o['value']:,.2f} €\nConfiance : {o['score']} %\n\nVérifier les pièces sources avant toute action."; expected=o['value']
            else:title=f"Analyser — {o['title']}"; draft=f"Marché : {o['title']}\nAcheteur : {o['buyer']}\nMatch Score : {o['score']}/100\nDate limite : {fmt_deadline(o['deadline']) or 'Non renseignée'}\n\nGO / NO-GO : vérifier lots, certifications, DCE, critères et capacité."; expected=0
        c.execute("INSERT INTO actions(opportunity_id,kind,title,draft,status,expected_value,created_at) VALUES(?,?,?,?, 'PENDING',?,?)",(item_id,kind,title,draft,expected,now())); c.commit(); c.close(); flash('Action préparée. Validation humaine requise.'); return redirect(url_for('actions'))

    @app.route('/actions/<int:aid>/status',methods=['POST'])
    @login_required
    def action_status(aid):
        st=request.form.get('status','PENDING').upper(); c=cx(); a=c.execute('SELECT * FROM actions WHERE id=?',(aid,)).fetchone()
        if not a: c.close(); return redirect(url_for('actions'))
        old=a['status']; c.execute('UPDATE actions SET status=? WHERE id=?',(st,aid)); c.commit(); c.close()
        log_status_change('ACTION',a['opportunity_id'],a['kind'],old,st,note=a['title'])
        return redirect(url_for('actions'))

    def parse_email_draft(draft):
        """Le format généré par create_action() est 'Objet : <sujet>\n\n<corps>'. Extrait les deux."""
        if draft.startswith('Objet :'):
            first_line,_,rest=draft.partition('\n')
            subject=first_line.replace('Objet :','',1).strip()
            body=rest.lstrip('\n')
            return subject,body
        return 'ProfitOS — relance',draft

    @app.route('/actions/<int:aid>/send',methods=['POST'])
    @login_required
    def action_send(aid):
        c=cx(); a=c.execute('SELECT * FROM actions WHERE id=?',(aid,)).fetchone()
        if not a: c.close(); abort(404)
        if a['status']!='APPROVED':
            c.close(); flash("Cette action doit d'abord être approuvée avant envoi."); return redirect(url_for('actions'))
        if a['kind']!='RECOVER':
            c.close(); flash("L'envoi par email n'est disponible que pour les actions RECOVER."); return redirect(url_for('actions'))
        inv=c.execute('SELECT * FROM invoices WHERE id=?',(a['opportunity_id'],)).fetchone(); c.close()
        if not inv or not inv['customer_email']:
            flash(f"Aucun email connu pour {inv['customer'] if inv else 'ce client'}. Ajoute une colonne \"customer_email\" dans ton fichier de factures pour activer l'envoi.")
            return redirect(url_for('actions'))
        subject,body=parse_email_draft(a['draft'])
        html='<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-size:14px;color:#111827;white-space:pre-wrap;">'+body.replace('<','&lt;').replace('>','&gt;')+'</div>'
        result=send_email(inv['customer_email'],subject,html)
        if result.get('dry_run'):
            flash(f"SMTP non configuré — email non envoyé réellement (mode simulation). Destinataire prévu : {inv['customer_email']}.")
            return redirect(url_for('actions'))
        c2=cx(); c2.execute("UPDATE actions SET status='SENT',sent_at=?,sent_to=? WHERE id=?",(now(),inv['customer_email'],aid)); c2.commit(); c2.close()
        log_status_change('ACTION',a['opportunity_id'],a['kind'],'APPROVED','SENT',note=f"Email envoyé à {inv['customer_email']}")
        flash(f"Email envoyé à {inv['customer_email']}."); return redirect(url_for('actions'))


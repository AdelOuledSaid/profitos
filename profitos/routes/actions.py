from profitos.runtime import *
from profitos.feature_access import requires_feature, requires_paid_plan


def register(app):
    def clean_legacy_text(value):
        if value is None:
            return ''
        text=str(value)
        try:
            repaired=text.encode('cp850').decode('utf-8')
        except (UnicodeEncodeError, UnicodeDecodeError):
            return text
        return repaired

    def present_action(row):
        x=dict(row)
        x['title']=clean_legacy_text(x.get('title'))
        x['draft']=clean_legacy_text(x.get('draft'))
        return x

    @app.route('/actions')
    @login_required
    @requires_paid_plan
    @require_area('actions')
    def actions():
        c=cx()
        active_rows=c.execute(
            "SELECT * FROM actions WHERE status IN ('PENDING','APPROVED') "
            "ORDER BY CASE status WHEN 'PENDING' THEN 0 ELSE 1 END,id DESC"
        ).fetchall()
        history_rows=c.execute(
            "SELECT * FROM actions WHERE status NOT IN ('PENDING','APPROVED') ORDER BY id DESC"
        ).fetchall()
        c.close()
        active_rows=[present_action(r) for r in active_rows]
        history_rows=[present_action(r) for r in history_rows]
        return render_template('actions.html',rows=active_rows,history_rows=history_rows)

    @app.route('/actions/create/<kind>/<int:item_id>',methods=['POST'])
    @login_required
    @requires_paid_plan
    def create_action(kind,item_id):
        kind=kind.upper()
        if not can_access(KIND_TO_AREA.get(kind,'')):
            flash("Votre rôle ne donne pas accès à cette section.")
            return redirect(url_for('actions'))

        force_new=request.form.get('force_new')=='1'
        c=cx()

        active_action=c.execute(
            "SELECT * FROM actions WHERE opportunity_id=? AND kind=? "
            "AND status IN ('PENDING','APPROVED') ORDER BY id DESC LIMIT 1",
            (item_id,kind)
        ).fetchone()
        if active_action:
            c.close()
            flash("Une action est déjà en cours pour cet élément. Ouvrez Action Center pour la gérer.")
            return redirect(url_for('detail',kind=kind,item_id=item_id))

        if kind=='RECOVER' and not force_new:
            last_sent=c.execute(
                "SELECT * FROM actions WHERE opportunity_id=? AND kind='RECOVER' "
                "AND status='SENT' ORDER BY COALESCE(sent_at,created_at) DESC,id DESC LIMIT 1",
                (item_id,)
            ).fetchone()
            if last_sent:
                c.close()
                sent_when=last_sent['sent_at'] or last_sent['created_at'] or ''
                suffix=f" le {sent_when[:16].replace('T',' ')}" if sent_when else ""
                flash("Une relance a déjà été envoyée"+suffix+". Utilisez « Nouvelle relance » si vous souhaitez relancer à nouveau.")
                return redirect(url_for('detail',kind=kind,item_id=item_id))

        if kind=='RECOVER':
            o=c.execute(
                'SELECT *,MAX(amount-paid_amount,0) outstanding FROM invoices WHERE id=?',
                (item_id,)
            ).fetchone()
            if not o:
                c.close()
                abort(404)
            if o['kind']=='RETENTION':
                title=f"Demander la levée de retenue — {o['customer']} — #{o['invoice_number']}"
                draft=(
                    f"Objet : Demande de levée de la retenue de garantie — Facture {o['invoice_number']}\n\n"
                    "Bonjour,\n\n"
                    f"La retenue de garantie de {o['outstanding']:,.2f} € appliquée sur la facture "
                    f"{o['invoice_number']} est libérable"
                    f"{' depuis le ' + o['retention_release_date'] if o['retention_release_date'] else ''}. "
                    "Pouvez-vous nous confirmer la date de virement ?\n\nCordialement"
                )
            else:
                title=f"Relancer {o['customer']} — #{o['invoice_number']}"
                draft=(
                    f"Objet : Relance facture {o['invoice_number']}\n\n"
                    "Bonjour,\n\n"
                    f"La facture {o['invoice_number']} présente un solde de {o['outstanding']:,.2f} € "
                    f"arrivé à échéance depuis {o['days_overdue']} jours. "
                    "Pouvez-vous nous confirmer sa date de règlement ?\n\nCordialement"
                )
            expected=o['outstanding']
        else:
            o=c.execute('SELECT * FROM opportunities WHERE id=? AND type=?',(item_id,kind)).fetchone()
            if not o:
                c.close()
                abort(404)
            if kind=='SAVE':
                title=f"Vérifier — {o['title']}"
                draft=(
                    f"Signal SAVE : {o['title']}\n"
                    f"Valeur potentielle : {o['value']:,.2f} €\n"
                    f"Confiance : {o['score']} %\n\n"
                    "Vérifier les pièces sources avant toute action."
                )
                expected=o['value']
            else:
                title=f"Analyser — {o['title']}"
                draft=(
                    f"Marché : {o['title']}\n"
                    f"Acheteur : {o['buyer']}\n"
                    f"Match Score : {o['score']}/100\n"
                    f"Date limite : {fmt_deadline(o['deadline']) or 'Non renseignée'}\n\n"
                    "GO / NO-GO : vérifier lots, certifications, DCE, critères et capacité."
                )
                expected=0

        c.execute(
            "INSERT INTO actions(opportunity_id,kind,title,draft,status,expected_value,created_at) "
            "VALUES(?,?,?,?, 'PENDING',?,?)",
            (item_id,kind,title,draft,expected,now())
        )
        c.commit()
        c.close()
        flash("Nouvelle relance préparée. Validation humaine requise." if kind=='RECOVER' and force_new else "Action préparée. Validation humaine requise.")
        return redirect(url_for('actions'))

    @app.route('/actions/<int:aid>/edit',methods=['POST'])
    @login_required
    @requires_paid_plan
    def action_edit(aid):
        c=cx()
        a=c.execute('SELECT * FROM actions WHERE id=?',(aid,)).fetchone()
        if not a:
            c.close()
            abort(404)
        if a['status'] not in ('PENDING','APPROVED','CANCELLED'):
            c.close()
            flash("Cette action ne peut plus être modifiée.")
            return redirect(url_for('actions'))

        title=clean_legacy_text(request.form.get('title','')).strip()
        draft=clean_legacy_text(request.form.get('draft','')).strip()
        if not title or not draft:
            c.close()
            flash("Le titre et le message sont obligatoires.")
            return redirect(url_for('actions'))

        c.execute('UPDATE actions SET title=?,draft=? WHERE id=?',(title,draft,aid))
        c.commit()
        c.close()
        log_activity('ACTION_EDIT',f'Action #{aid} modifiée')
        flash("Action modifiée.")
        return redirect(url_for('actions'))

    @app.route('/actions/<int:aid>/delete',methods=['POST'])
    @login_required
    @requires_paid_plan
    def action_delete(aid):
        c=cx()
        a=c.execute('SELECT * FROM actions WHERE id=?',(aid,)).fetchone()
        if not a:
            c.close()
            return redirect(url_for('actions'))
        if a['status']!='CANCELLED':
            c.close()
            flash("Seules les actions annulées peuvent être supprimées.")
            return redirect(url_for('actions'))

        c.execute('DELETE FROM actions WHERE id=?',(aid,))
        c.commit()
        c.close()
        log_activity('ACTION_DELETE',f"Action annulée supprimée : {clean_legacy_text(a['title'])}")
        flash("Action supprimée définitivement.")
        return redirect(url_for('actions'))

    @app.route('/actions/<int:aid>/status',methods=['POST'])
    @login_required
    @requires_paid_plan
    def action_status(aid):
        st=request.form.get('status','PENDING').upper()
        allowed={'PENDING','APPROVED','CANCELLED','DONE'}
        if st not in allowed:
            flash("Statut d'action invalide.")
            return redirect(url_for('actions'))

        c=cx()
        a=c.execute('SELECT * FROM actions WHERE id=?',(aid,)).fetchone()
        if not a:
            c.close()
            return redirect(url_for('actions'))

        if st=='PENDING' and a['status']=='CANCELLED':
            duplicate=c.execute(
                "SELECT id FROM actions WHERE opportunity_id=? AND kind=? AND id<>? "
                "AND status IN ('PENDING','APPROVED') LIMIT 1",
                (a['opportunity_id'],a['kind'],aid)
            ).fetchone()
            if duplicate:
                c.close()
                flash("Impossible de réactiver : une autre action est déjà active pour cet élément.")
                return redirect(url_for('actions'))

        old=a['status']
        c.execute('UPDATE actions SET status=? WHERE id=?',(st,aid))
        c.commit()
        c.close()
        log_status_change('ACTION',a['opportunity_id'],a['kind'],old,st,note=clean_legacy_text(a['title']))

        if st=='CANCELLED':
            flash("Action annulée et déplacée dans l'historique.")
        elif old=='CANCELLED' and st=='PENDING':
            flash("Action réactivée.")
        return redirect(url_for('actions'))

    def parse_email_draft(draft):
        draft=clean_legacy_text(draft)
        if draft.startswith('Objet :'):
            first_line,_,rest=draft.partition('\n')
            return first_line.replace('Objet :','',1).strip(),rest.lstrip('\n')
        return 'ProfitOS — relance',draft

    @app.route('/actions/<int:aid>/send',methods=['POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @requires_feature('advanced_features')
    def action_send(aid):
        c=cx()
        a=c.execute('SELECT * FROM actions WHERE id=?',(aid,)).fetchone()
        if not a:
            c.close()
            abort(404)
        if a['status']!='APPROVED':
            c.close()
            flash("Cette action doit d'abord être approuvée avant envoi.")
            return redirect(url_for('actions'))
        if a['kind']!='RECOVER':
            c.close()
            flash("L'envoi par email n'est disponible que pour les actions RECOVER.")
            return redirect(url_for('actions'))

        inv=c.execute('SELECT * FROM invoices WHERE id=?',(a['opportunity_id'],)).fetchone()
        c.close()
        if not inv or not inv['customer_email']:
            flash(
                f"Aucun email connu pour {inv['customer'] if inv else 'ce client'}. "
                'Ajoute une colonne "customer_email" dans ton fichier de factures pour activer l’envoi.'
            )
            return redirect(url_for('actions'))

        subject,body=parse_email_draft(a['draft'])
        safe_body=body.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
        html=(
            '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
            'font-size:14px;color:#111827;white-space:pre-wrap;">'+safe_body+'</div>'
        )
        result=send_email(inv['customer_email'],subject,html)

        if not result.get('sent'):
            if result.get('dry_run'):
                flash(f"Service email non configuré — email non envoyé réellement (mode simulation). Destinataire prévu : {inv['customer_email']}.")
            else:
                flash("Échec de l'envoi de l'email. L'action reste approuvée et peut être réessayée.")
            return redirect(url_for('actions'))

        c2=cx()
        c2.execute("UPDATE actions SET status='SENT',sent_at=?,sent_to=? WHERE id=?",(now(),inv['customer_email'],aid))
        c2.commit()
        c2.close()

        log_status_change('ACTION',a['opportunity_id'],a['kind'],'APPROVED','SENT',note=f"Email envoyé à {inv['customer_email']}")
        flash(f"Email envoyé à {inv['customer_email']}.")
        return redirect(url_for('actions'))

    @app.route('/actions/<int:aid>/send-sms',methods=['POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @requires_feature('advanced_features')
    def action_send_sms(aid):
        c=cx()
        a=c.execute('SELECT * FROM actions WHERE id=?',(aid,)).fetchone()
        if not a:
            c.close(); abort(404)
        if a['status']!='APPROVED':
            c.close(); flash("Cette action doit d'abord être approuvée avant envoi."); return redirect(url_for('actions'))
        if a['kind']!='RECOVER':
            c.close(); flash("L'envoi par SMS n'est disponible que pour les actions RECOVER."); return redirect(url_for('actions'))

        inv=c.execute('SELECT * FROM invoices WHERE id=?',(a['opportunity_id'],)).fetchone()
        c.close()
        if not inv or not inv['customer_phone']:
            flash(
                f"Aucun téléphone connu pour {inv['customer'] if inv else 'ce client'}. "
                'Ajoute une colonne "customer_phone" dans ton fichier de factures pour activer l’envoi.'
            )
            return redirect(url_for('actions'))

        subject,body=parse_email_draft(a['draft'])
        sms_body=f"{subject} — {body.split(chr(10))[0][:140]}"  # SMS court : objet + première ligne du message
        result=send_sms(inv['customer_phone'],sms_body)

        if not result.get('sent'):
            if result.get('dry_run'):
                flash(f"Service SMS non configuré — SMS non envoyé réellement (mode simulation). Destinataire prévu : {inv['customer_phone']}.")
            else:
                flash(f"Échec de l'envoi du SMS ({result.get('error','erreur inconnue')}). L'action reste approuvée et peut être réessayée.")
            return redirect(url_for('actions'))

        c2=cx()
        c2.execute("UPDATE actions SET status='SENT',sent_at=?,sent_to=? WHERE id=?",(now(),inv['customer_phone'],aid))
        c2.commit()
        c2.close()

        log_status_change('ACTION',a['opportunity_id'],a['kind'],'APPROVED','SENT',note=f"SMS envoyé à {inv['customer_phone']}")
        flash(f"SMS envoyé à {inv['customer_phone']}.")
        return redirect(url_for('actions'))


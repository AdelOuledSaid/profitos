from profitos.runtime import *
from profitos.feature_access import requires_feature, requires_paid_plan


def register(app):
    def clean_legacy_text(value):
        """Répare d'anciens textes UTF-8 qui ont été décodés avec CP850."""
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
        return render_template(
            'actions.html',
            rows=active_rows,
            history_rows=history_rows,
        )

    @app.route('/actions/create/<kind>/<int:item_id>',methods=['POST'])
    @login_required
    @requires_paid_plan
    def create_action(kind,item_id):
        kind=kind.upper()
        if not can_access(KIND_TO_AREA.get(kind,'')):
            flash("Votre rôle ne donne pas accès à cette section.")
            return redirect(url_for('actions'))

        c=cx()
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
                    "Pouvez-vous nous confirmer la date de virement ?\n\n"
                    "Cordialement"
                )
            else:
                title=f"Relancer {o['customer']} — #{o['invoice_number']}"
                draft=(
                    f"Objet : Relance facture {o['invoice_number']}\n\n"
                    "Bonjour,\n\n"
                    f"La facture {o['invoice_number']} présente un solde de {o['outstanding']:,.2f} € "
                    f"arrivé à échéance depuis {o['days_overdue']} jours. "
                    "Pouvez-vous nous confirmer sa date de règlement ?\n\n"
                    "Cordialement"
                )
            expected=o['outstanding']
        else:
            o=c.execute(
                'SELECT * FROM opportunities WHERE id=? AND type=?',
                (item_id,kind)
            ).fetchone()
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
        flash('Action préparée. Validation humaine requise.')
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

        title=clean_legacy_text(a['title'])
        c.execute('DELETE FROM actions WHERE id=?',(aid,))
        c.commit()
        c.close()
        log_activity('ACTION_DELETE',f'Action annulée supprimée : {title}')
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
            subject=first_line.replace('Objet :','',1).strip()
            body=rest.lstrip('\n')
            return subject,body
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
            'font-size:14px;color:#111827;white-space:pre-wrap;">'
            + safe_body + '</div>'
        )
        result=send_email(inv['customer_email'],subject,html)

        if not result.get('sent'):
            if result.get('dry_run'):
                flash(
                    f"Service email non configuré — email non envoyé réellement "
                    f"(mode simulation). Destinataire prévu : {inv['customer_email']}."
                )
            else:
                flash("Échec de l'envoi de l'email. L'action reste approuvée et peut être réessayée.")
            return redirect(url_for('actions'))

        c2=cx()
        c2.execute(
            "UPDATE actions SET status='SENT',sent_at=?,sent_to=? WHERE id=?",
            (now(),inv['customer_email'],aid)
        )
        c2.commit()
        c2.close()

        log_status_change(
            'ACTION',a['opportunity_id'],a['kind'],'APPROVED','SENT',
            note=f"Email envoyé à {inv['customer_email']}"
        )
        flash(f"Email envoyé à {inv['customer_email']}.")
        return redirect(url_for('actions'))

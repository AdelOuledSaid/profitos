from profitos.runtime import *
from profitos.plan_usage import quota_state, record_usage
from profitos.feature_access import requires_paid_plan


def register(app):
    @app.route('/reports/weekly')
    @login_required
    @require_area('weekly')
    def weekly_report_preview():
        org=current_org(); digest=compute_weekly_digest(org['id'])
        return render_template('email_weekly.html',org=org,digest=digest,app_url=request.host_url.rstrip('/'),preview=True)

    def pdf_safe(text):
        if text is None:return ''
        text=str(text)
        replacements={'—':'-','–':'-','\u2018':"'",'\u2019':"'",'\u201c':'"','\u201d':'"',
                      '…':'...','\xa0':' ','•':'-','€':'EUR'}
        for src,dst in replacements.items():
            text=text.replace(src,dst)
        return text.encode('latin-1',errors='replace').decode('latin-1')

    @app.route('/forecast')
    @login_required
    @requires_active_plan
    @require_area('impact')
    def cash_forecast():
        """Prévision de trésorerie à 90 jours — estimation indicative, pas une garantie.
        RECOVER : pondéré par le score (proxy de probabilité de recouvrement), classé dans
        un horizon 0-30/31-60/61-90j selon l'ancienneté du retard actuel (plus une créance
        est déjà ancienne, plus son horizon de résolution est repoussé, par prudence).
        SAVE : pondéré par le score, classé selon la facilité présumée de mise en œuvre.
        GROW : exclu du chiffrage 90 jours (cycle de marché public trop long), affiché à part
        comme pipeline indicatif au-delà de l'horizon."""
        # Même base que Trésorerie & scénarios (scénario probable) : créances émises ET importées,
        # factures fournisseurs restant à payer (à leur échéance), dépenses futures, dépense
        # courante observée. Les économies potentielles ne sont PAS des encaissements : elles
        # sont affichées à part et jamais ajoutées aux flux.
        from profitos.routes.cash_intelligence import build_cash_intelligence
        ci=build_cash_intelligence()
        today=date.today()
        labels=('0-30j','31-60j','61-90j')
        bounds={'0-30j':(1,30),'31-60j':(31,60),'61-90j':(61,90)}
        inflows={k:0.0 for k in labels}; supplier_out={k:0.0 for k in labels}
        expense_out={k:0.0 for k in labels}; burn={k:0.0 for k in labels}; net={k:0.0 for k in labels}
        # Mêmes flux et même courbe que Trésorerie & scénarios (scénario probable) : avec un
        # solde de départ, le flux net de chaque période est exactement l'écart de la courbe.
        from profitos.routes.cash_intelligence import _curve_events
        events=_curve_events(ci['receivables'],ci['scheduled_outflows'],ci.get('inflows'),'probable',None,today)
        probable=next((cv for cv in ci['curves'] if cv['mode']=='probable'),None)
        for k,(a,b) in bounds.items():
            for day,amount,cat in events:
                if a<=day<=b:
                    if amount>0: inflows[k]+=amount
                    elif cat=='suppliers': supplier_out[k]-=amount
                    else: expense_out[k]-=amount
            if probable:
                net[k]=round(probable['values'][b]-probable['values'][a-1],2)
                burn[k]=round(inflows[k]-supplier_out[k]-expense_out[k]-net[k],2)
            else:
                burn[k]=round(ci['daily_burn']*(b-a+1),2)
                net[k]=round(inflows[k]-supplier_out[k]-expense_out[k]-burn[k],2)
            inflows[k]=round(inflows[k],2); supplier_out[k]=round(supplier_out[k],2); expense_out[k]=round(expense_out[k],2)
        cumulative=[]; running=0.0
        for k in labels:
            running+=net[k]; cumulative.append((k,round(running,2)))
        c=cx()
        saves=c.execute("SELECT title,value FROM opportunities WHERE type='SAVE' AND status='OPEN'").fetchall()
        grow_total=c.execute("SELECT COUNT(*) n FROM opportunities WHERE type='GROW' AND status='OPEN'").fetchone()['n']
        c.close()
        from profitos.routes.money_hunter import split_save_totals
        save_one_off,save_annual=split_save_totals(saves)
        chart=bars_svg([(k,net[k]) for k in labels])
        return render_template('forecast.html',ci=ci,labels=labels,inflows=inflows,supplier_out=supplier_out,
            expense_out=expense_out,burn=burn,net=net,cumulative=cumulative,total_90j=round(running,2),chart=chart,
            grow_total=grow_total,save_count=len(saves),save_one_off=save_one_off,save_annual=save_annual,
            invoice_count=len(ci['receivables_in_horizon']),other_inflows=len(ci.get('inflows') or []),
            gross_expected=round(sum(inflows.values()),2),
            planned_outflows=round(sum(supplier_out.values())+sum(expense_out.values()),2),
            supplier_payables_total=round(sum(supplier_out.values()),2))

    @app.route('/reports/monthly.pdf')
    @login_required
    @requires_active_plan
    @requires_paid_plan
    def monthly_report_pdf():
        org=current_org()
        quota=quota_state('reports_per_month',organization_id=org['id'],plan=org['plan'])
        if not quota['allowed']:
            flash(
                f"Quota mensuel de rapports atteint pour la formule {org['plan']} "
                f"({quota['used']}/{quota['limit']}). Passez à une formule supérieure."
            )
            return redirect(url_for('impact'))

        try:
            from fpdf import FPDF
        except ImportError:
            flash("La génération du rapport PDF est momentanément indisponible. Réessayez plus tard ou contactez le support.")
            return redirect(url_for('impact'))

        c=cx()
        from profitos.receivables import open_receivables
        overdue_rows=sorted((r for r in open_receivables(c) if r['overdue']),key=lambda r:r['score'],reverse=True)
        recover={'t':sum(r['outstanding'] for r in overdue_rows),'n':len(overdue_rows)}
        save=c.execute("SELECT COALESCE(SUM(value),0) t,COUNT(*) n FROM opportunities WHERE type='SAVE' AND status='OPEN'").fetchone()
        grow=c.execute("SELECT COUNT(*) n FROM opportunities WHERE type='GROW' AND status='OPEN'").fetchone()
        verified=c.execute("SELECT COALESCE(SUM(amount),0) t FROM outcomes WHERE verified=1").fetchone()
        top_recover=overdue_rows[:8]
        top_save=c.execute("SELECT title,value FROM opportunities WHERE type='SAVE' AND status='OPEN' ORDER BY score DESC LIMIT 5").fetchall()
        top_grow=c.execute("SELECT title,buyer FROM opportunities WHERE type='GROW' AND status='OPEN' ORDER BY score DESC LIMIT 5").fetchall()
        c.close()

        period=fr_month(datetime.now(PARIS).strftime('%Y-%m'))
        pdf=FPDF(orientation='P',unit='mm',format='A4')
        pdf.set_auto_page_break(auto=True,margin=18)
        pdf.add_page()

        pdf.set_font('Helvetica','B',20); pdf.set_text_color(17,24,39)
        pdf.cell(0,12,'ProfitOS - Rapport mensuel',ln=1)
        pdf.set_font('Helvetica','',12); pdf.set_text_color(107,114,128)
        pdf.cell(0,8,pdf_safe(f"{org['name']} - {period}"),ln=1)
        pdf.ln(6)

        def kpi(label,value,color):
            pdf.set_text_color(*color); pdf.set_font('Helvetica','B',16)
            pdf.cell(63,10,value,border=0)

        pdf.set_font('Helvetica','',9); pdf.set_text_color(107,114,128)
        pdf.cell(63,6,'À ENCAISSER'); pdf.cell(63,6,'ÉCONOMIES POTENTIELLES'); pdf.cell(63,6,'MARCHÉS PUBLICS'); pdf.ln(6)
        kpi('recover',pdf_safe(f"{fr_number(recover['t'])} EUR"),(220,38,38))
        kpi('save',pdf_safe(f"{fr_number(save['t'])} EUR/an"),(217,119,6))
        kpi('grow',f"{grow['n']} opportunites",(22,163,74))
        pdf.ln(14)
        pdf.set_font('Helvetica','',10); pdf.set_text_color(75,85,99)
        pdf.cell(0,6,pdf_safe(f"Impact verifie a date : {fr_number(verified['t'])} EUR"),ln=1)
        pdf.ln(6)

        def section(title):
            pdf.set_font('Helvetica','B',13); pdf.set_text_color(17,24,39); pdf.cell(0,10,title,ln=1)
            pdf.set_draw_color(229,231,235); pdf.line(pdf.get_x(),pdf.get_y(),pdf.get_x()+180,pdf.get_y()); pdf.ln(3)

        section('Relances clients - créances prioritaires')
        if top_recover:
            pdf.set_font('Helvetica','',10); pdf.set_text_color(31,41,55)
            for r in top_recover:
                pdf.cell(0,7,pdf_safe(f"- {r['customer']} (#{r['invoice_number']}) - {fr_number(r['outstanding'])} EUR - {r['days_overdue']} j de retard"),ln=1)
        else:
            pdf.set_font('Helvetica','I',10); pdf.set_text_color(156,163,175); pdf.cell(0,7,'Aucune creance en retard.',ln=1)
        pdf.ln(4)

        section('Économies détectées')
        if top_save:
            pdf.set_font('Helvetica','',10); pdf.set_text_color(31,41,55)
            for s in top_save:
                pdf.cell(0,7,pdf_safe(f"- {s['title']} - {fr_number(s['value'])} EUR/an"),ln=1)
        else:
            pdf.set_font('Helvetica','I',10); pdf.set_text_color(156,163,175); pdf.cell(0,7,'Aucune économie détectée.',ln=1)
        pdf.ln(4)

        section('Marchés publics - opportunités')
        if top_grow:
            pdf.set_font('Helvetica','',10); pdf.set_text_color(31,41,55)
            for g in top_grow:
                pdf.cell(0,7,pdf_safe(f"- {g['title']} - {g['buyer'] or ''}"),ln=1)
        else:
            pdf.set_font('Helvetica','I',10); pdf.set_text_color(156,163,175); pdf.cell(0,7,'Aucun marché public ouvert.',ln=1)

        pdf_bytes=bytes(pdf.output(dest='S'))
        filename=f"profitos-rapport-{datetime.now(timezone.utc).strftime('%Y-%m')}.pdf"

        record_usage('reports_per_month',organization_id=org['id'])

        from flask import Response
        return Response(pdf_bytes,mimetype='application/pdf',
            headers={'Content-Disposition':f'attachment; filename="{filename}"'})

    @app.route('/reports/weekly/send',methods=['POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    def weekly_report_send():
        org=current_org()
        quota=quota_state('reports_per_month',organization_id=org['id'],plan=org['plan'])
        if not quota['allowed']:
            flash(
                f"Quota mensuel de rapports atteint pour la formule {org['plan']} "
                f"({quota['used']}/{quota['limit']}). Passez à une formule supérieure."
            )
            return redirect(url_for('weekly_report_preview'))

        digest=compute_weekly_digest(org['id'])
        result=send_weekly_email(org,digest)

        if result.get('sent'):
            record_usage('reports_per_month',organization_id=org['id'])
            flash(f"Rapport hebdomadaire envoyé à {result['to']}.")
        elif result.get('dry_run'):
            flash(f"Service e-mail non configuré — e-mail non envoyé (mode simulation). Destinataire prévu : {result['to']}.")
        elif result.get('reason')=='no_owner_email':
            flash("Impossible d'envoyer le rapport : aucun propriétaire avec e-mail trouvé.")
        else:
            flash("Impossible d'envoyer le rapport pour le moment. Réessayez dans quelques minutes.")
        return redirect(url_for('weekly_report_preview'))

    @app.route('/impact',methods=['GET','POST'])
    @login_required
    @require_area('impact')
    def impact():
        from profitos.entities import current_entity_id
        c=cx(); eid=current_entity_id()
        if request.method=='POST':
            aid=int(request.form['action_id']); typ=request.form['outcome_type']; amount=float(request.form['amount']); ver=1 if request.form.get('verified') else 0; note=request.form.get('note','')
            a=c.execute('SELECT * FROM actions WHERE id=? AND entity_id IS ?',(aid,eid)).fetchone()
            if not a:
                c.close(); abort(404)
            c.execute('INSERT INTO outcomes(action_id,outcome_type,amount,verified,note,created_at) VALUES(?,?,?,?,?,?)',(aid,typ,amount,ver,note,now()))
            c.execute("UPDATE actions SET status='DONE' WHERE id=? AND entity_id IS ?",(aid,eid))
            c.commit()
            if a:log_status_change('ACTION',a['opportunity_id'],a['kind'],a['status'],'DONE',note=f'{typ} — {fr_number(amount)} €')
            flash('Résultat enregistré.')
        rows=c.execute('SELECT outcomes.*,actions.title action_title FROM outcomes JOIN actions ON actions.id=outcomes.action_id WHERE actions.entity_id IS ? ORDER BY outcomes.id DESC',(eid,)).fetchall()
        eligible=c.execute("SELECT * FROM actions WHERE entity_id IS ? AND status IN ('APPROVED','DONE') ORDER BY id DESC",(eid,)).fetchall()
        verified=c.execute('SELECT COALESCE(SUM(o.amount),0) t FROM outcomes o JOIN actions a ON a.id=o.action_id WHERE o.verified=1 AND a.entity_id IS ?',(eid,)).fetchone()['t']
        c.close()
        return render_template('impact.html',rows=rows,eligible=eligible,verified=verified)

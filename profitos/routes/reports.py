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
        c=cx()
        invoices=c.execute("SELECT *,MAX(amount-paid_amount,0) outstanding FROM invoices WHERE LOWER(COALESCE(status,''))!='paid' AND days_overdue>0").fetchall()
        saves=c.execute("SELECT * FROM opportunities WHERE type='SAVE' AND status='OPEN'").fetchall()
        grow_total=c.execute("SELECT COALESCE(SUM(1),0) n FROM opportunities WHERE type='GROW' AND status='OPEN'").fetchone()['n']
        future_expenses=c.execute(
            "SELECT vendor,description,category,amount,expense_date FROM expenses "
            "WHERE expense_date IS NOT NULL AND expense_date>? AND expense_date<=? ORDER BY expense_date" ,
            (date.today().isoformat(),(date.today()+timedelta(days=90)).isoformat())
        ).fetchall()
        c.close()

        buckets={'0-30j':0.0,'31-60j':0.0,'61-90j':0.0}
        for inv in invoices:
            weight=(inv['score'] or 0)/100
            expected=inv['outstanding']*weight
            days=inv['days_overdue'] or 0
            if days<=30: buckets['0-30j']+=expected
            elif days<=60: buckets['31-60j']+=expected
            else: buckets['61-90j']+=expected  # créance déjà ancienne : horizon repoussé par prudence

        for s in saves:
            weight=(s['score'] or 0)/100
            expected=(s['value'] or 0)*weight
            if s['score']>=80: buckets['0-30j']+=expected
            elif s['score']>=50: buckets['31-60j']+=expected
            else: buckets['61-90j']+=expected

        gross_expected=sum(buckets.values())
        obligation_buckets={'0-30j':0.0,'31-60j':0.0,'61-90j':0.0}
        today=date.today()
        for e in future_expenses:
            try: d=datetime.strptime(str(e['expense_date'])[:10],'%Y-%m-%d').date()
            except Exception: continue
            days=(d-today).days
            amount=max(0.0,float(e['amount'] or 0))
            if days<=30: key='0-30j'
            elif days<=60: key='31-60j'
            else: key='61-90j'
            obligation_buckets[key]+=amount
            buckets[key]-=amount

        planned_outflows=sum(obligation_buckets.values())
        total_90j=sum(buckets.values())
        chart=bars_svg(list(buckets.items()))
        cumulative=[]
        running=0
        for label,v in buckets.items():
            running+=v; cumulative.append((label,round(running)))

        return render_template('forecast.html',buckets=buckets,total_90j=total_90j,chart=chart,
            cumulative=cumulative,grow_total=grow_total,
            invoice_count=len(invoices),save_count=len(saves),
            gross_expected=gross_expected,planned_outflows=planned_outflows,
            obligation_buckets=obligation_buckets,future_expense_count=len(future_expenses))

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
            flash("La génération PDF nécessite le paquet 'fpdf2' — lance : pip install -r requirements.txt")
            return redirect(url_for('impact'))

        c=cx()
        recover=c.execute("SELECT COALESCE(SUM(MAX(amount-paid_amount,0)),0) t,COUNT(*) n FROM invoices WHERE LOWER(COALESCE(status,''))!='paid' AND days_overdue>0").fetchone()
        save=c.execute("SELECT COALESCE(SUM(value),0) t,COUNT(*) n FROM opportunities WHERE type='SAVE' AND status='OPEN'").fetchone()
        grow=c.execute("SELECT COUNT(*) n FROM opportunities WHERE type='GROW' AND status='OPEN'").fetchone()
        verified=c.execute("SELECT COALESCE(SUM(amount),0) t FROM outcomes WHERE verified=1").fetchone()
        top_recover=c.execute("SELECT invoice_number,customer,MAX(amount-paid_amount,0) outstanding,days_overdue FROM invoices WHERE LOWER(COALESCE(status,''))!='paid' AND days_overdue>0 ORDER BY score DESC LIMIT 8").fetchall()
        top_save=c.execute("SELECT title,value FROM opportunities WHERE type='SAVE' AND status='OPEN' ORDER BY score DESC LIMIT 5").fetchall()
        top_grow=c.execute("SELECT title,buyer FROM opportunities WHERE type='GROW' AND status='OPEN' ORDER BY score DESC LIMIT 5").fetchall()
        c.close()

        period=datetime.now(timezone.utc).strftime('%B %Y')
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
        pdf.cell(63,6,'RECOVERABLE'); pdf.cell(63,6,'POTENTIAL SAVINGS'); pdf.cell(63,6,'GROW'); pdf.ln(6)
        kpi('recover',f"{recover['t']:,.0f} EUR",(220,38,38))
        kpi('save',f"{save['t']:,.0f} EUR/an",(217,119,6))
        kpi('grow',f"{grow['n']} opportunites",(22,163,74))
        pdf.ln(14)
        pdf.set_font('Helvetica','',10); pdf.set_text_color(75,85,99)
        pdf.cell(0,6,f"Impact verifie a date : {verified['t']:,.0f} EUR",ln=1)
        pdf.ln(6)

        def section(title):
            pdf.set_font('Helvetica','B',13); pdf.set_text_color(17,24,39); pdf.cell(0,10,title,ln=1)
            pdf.set_draw_color(229,231,235); pdf.line(pdf.get_x(),pdf.get_y(),pdf.get_x()+180,pdf.get_y()); pdf.ln(3)

        section('RECOVER - creances prioritaires')
        if top_recover:
            pdf.set_font('Helvetica','',10); pdf.set_text_color(31,41,55)
            for r in top_recover:
                pdf.cell(0,7,pdf_safe(f"- {r['customer']} (#{r['invoice_number']}) - {r['outstanding']:,.0f} EUR - {r['days_overdue']} j de retard"),ln=1)
        else:
            pdf.set_font('Helvetica','I',10); pdf.set_text_color(156,163,175); pdf.cell(0,7,'Aucune creance en retard.',ln=1)
        pdf.ln(4)

        section('SAVE - economies detectees')
        if top_save:
            pdf.set_font('Helvetica','',10); pdf.set_text_color(31,41,55)
            for s in top_save:
                pdf.cell(0,7,pdf_safe(f"- {s['title']} - {s['value']:,.0f} EUR/an"),ln=1)
        else:
            pdf.set_font('Helvetica','I',10); pdf.set_text_color(156,163,175); pdf.cell(0,7,'Aucun signal SAVE ouvert.',ln=1)
        pdf.ln(4)

        section('GROW - opportunites de marche')
        if top_grow:
            pdf.set_font('Helvetica','',10); pdf.set_text_color(31,41,55)
            for g in top_grow:
                pdf.cell(0,7,pdf_safe(f"- {g['title']} - {g['buyer'] or ''}"),ln=1)
        else:
            pdf.set_font('Helvetica','I',10); pdf.set_text_color(156,163,175); pdf.cell(0,7,'Aucune opportunite GROW ouverte.',ln=1)

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
            flash(f"Service email non configuré — email non envoyé (mode simulation). Destinataire prévu : {result['to']}.")
        elif result.get('reason')=='no_owner_email':
            flash("Impossible d'envoyer le rapport : aucun propriétaire avec email trouvé.")
        else:
            flash("Impossible d'envoyer le rapport pour le moment. Réessayez dans quelques minutes.")
        return redirect(url_for('weekly_report_preview'))

    @app.route('/impact',methods=['GET','POST'])
    @login_required
    @require_area('impact')
    def impact():
        c=cx()
        if request.method=='POST':
            aid=int(request.form['action_id']); typ=request.form['outcome_type']; amount=float(request.form['amount']); ver=1 if request.form.get('verified') else 0; note=request.form.get('note','')
            c.execute('INSERT INTO outcomes(action_id,outcome_type,amount,verified,note,created_at) VALUES(?,?,?,?,?,?)',(aid,typ,amount,ver,note,now()))
            a=c.execute('SELECT * FROM actions WHERE id=?',(aid,)).fetchone()
            c.execute("UPDATE actions SET status='DONE' WHERE id=?",(aid,))
            c.commit()
            if a:log_status_change('ACTION',a['opportunity_id'],a['kind'],a['status'],'DONE',note=f'{typ} — {amount:,.0f} €')
            flash('Résultat enregistré.')
        rows=c.execute('SELECT outcomes.*,actions.title action_title FROM outcomes LEFT JOIN actions ON actions.id=outcomes.action_id ORDER BY outcomes.id DESC').fetchall()
        eligible=c.execute("SELECT * FROM actions WHERE status IN ('APPROVED','DONE') ORDER BY id DESC").fetchall()
        verified=c.execute('SELECT COALESCE(SUM(amount),0) t FROM outcomes WHERE verified=1').fetchone()['t']
        c.close()
        return render_template('impact.html',rows=rows,eligible=eligible,verified=verified)

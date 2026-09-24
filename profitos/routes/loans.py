from profitos.runtime import *
from profitos.loans import create_loan, pay_installment, compute_monthly_payment
from profitos.accounting import AccountingError


def register(app):
    @app.route('/comptabilite/emprunts')
    @login_required
    def loans_list():
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        ef = 'entity_id=?' if eid else 'entity_id IS NULL'
        ep = (eid,) if eid else ()
        c = cx()
        loans = c.execute(f"SELECT * FROM loans WHERE {ef} ORDER BY start_date DESC", ep).fetchall()
        remaining = {}
        for loan in loans:
            row = c.execute(
                "SELECT COALESCE(SUM(capital_amount),0) t FROM loan_installments WHERE loan_id=? AND paid=0",
                (loan['id'],),
            ).fetchone()
            remaining[loan['id']] = row['t']
        c.close()
        return render_template('loans_list.html', loans=loans, remaining=remaining)

    @app.route('/comptabilite/emprunts/nouveau', methods=['GET', 'POST'])
    @login_required
    def loan_new():
        from profitos.entities import current_entity_id
        if request.method == 'POST':
            lender_name = (request.form.get('lender_name') or '').strip()
            try:
                principal_amount = float(request.form.get('principal_amount') or 0)
                annual_rate = float(request.form.get('annual_rate') or 0)
                duration_months = int(request.form.get('duration_months') or 0)
            except ValueError:
                flash("Montant, taux et durée doivent être des nombres valides.")
                return redirect(url_for('loan_new'))
            start_date = request.form.get('start_date') or date.today().isoformat()
            if not lender_name or principal_amount <= 0 or duration_months <= 0:
                flash("Le prêteur, le montant emprunté et la durée sont obligatoires.")
                return redirect(url_for('loan_new'))

            c = cx()
            loan_id = create_loan(
                c, lender_name, principal_amount, annual_rate, start_date, duration_months,
                entity_id=current_entity_id(), notes=(request.form.get('notes') or '').strip(),
                created_by=current_user()['email'],
            )
            c.close()
            log_activity('LOAN_CREATED', f"Emprunt créé : {lender_name} ({fr_number(principal_amount,2)} €)")
            flash("Emprunt enregistré avec son échéancier complet.")
            return redirect(url_for('loan_detail', loan_id=loan_id))

        return render_template('loan_new.html', today=date.today().isoformat())

    @app.route('/comptabilite/emprunts/<int:loan_id>')
    @login_required
    def loan_detail(loan_id):
        c = cx()
        loan = c.execute('SELECT * FROM loans WHERE id=?', (loan_id,)).fetchone()
        if not loan:
            c.close(); abort(404)
        installments = c.execute(
            'SELECT * FROM loan_installments WHERE loan_id=? ORDER BY installment_number', (loan_id,)
        ).fetchall()
        c.close()
        paid_capital = sum(i['capital_amount'] for i in installments if i['paid'])
        paid_interest = sum(i['interest_amount'] for i in installments if i['paid'])
        return render_template('loan_detail.html', loan=loan, installments=installments,
                                paid_capital=paid_capital, paid_interest=paid_interest)

    @app.route('/comptabilite/emprunts/echeance/<int:installment_id>/payer', methods=['POST'])
    @login_required
    def loan_installment_pay(installment_id):
        from profitos.entities import current_entity_id
        c = cx()
        installment = c.execute('SELECT loan_id FROM loan_installments WHERE id=?', (installment_id,)).fetchone()
        if not installment:
            c.close(); abort(404)
        try:
            pay_installment(c, installment_id, entity_id=current_entity_id(), created_by=current_user()['email'])
        except AccountingError as e:
            c.close()
            flash(f"Règlement impossible : {e}")
            return redirect(url_for('loan_detail', loan_id=installment['loan_id']))
        c.close()
        log_activity('LOAN_INSTALLMENT_PAID', f"Échéance d'emprunt #{installment_id} réglée")
        flash("Échéance réglée, écriture comptable générée.")
        return redirect(url_for('loan_detail', loan_id=installment['loan_id']))

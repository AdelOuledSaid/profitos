from profitos.runtime import *
from profitos.finance_leases import (
    create_finance_lease, pay_lease_payment, remaining_commitment,
    total_remaining_commitment, exercise_purchase_option,
)
from profitos.accounting import AccountingError


def register(app):
    @app.route('/comptabilite/credit-bail')
    @login_required
    def finance_leases_list():
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        ef = 'entity_id=?' if eid else 'entity_id IS NULL'
        ep = (eid,) if eid else ()
        c = cx()
        leases = c.execute(f"SELECT * FROM finance_leases WHERE {ef} ORDER BY start_date DESC", ep).fetchall()
        remaining = {l['id']: remaining_commitment(c, l['id']) for l in leases}
        total_engagement = total_remaining_commitment(c, eid)
        c.close()
        return render_template('finance_leases_list.html', leases=leases, remaining=remaining,
                                total_engagement=total_engagement)

    @app.route('/comptabilite/credit-bail/nouveau', methods=['GET', 'POST'])
    @login_required
    def finance_lease_new():
        from profitos.entities import current_entity_id
        if request.method == 'POST':
            lessor_name = (request.form.get('lessor_name') or '').strip()
            asset_description = (request.form.get('asset_description') or '').strip()
            periodicity = request.form.get('periodicity') or 'monthly'
            try:
                asset_value = float(request.form.get('asset_value') or 0)
                redevance_amount = float(request.form.get('redevance_amount') or 0)
                duration_months = int(request.form.get('duration_months') or 0)
                purchase_option_amount = float(request.form.get('purchase_option_amount') or 0)
            except ValueError:
                flash("Les montants et la durée doivent être des nombres valides.")
                return redirect(url_for('finance_lease_new'))
            start_date = request.form.get('start_date') or date.today().isoformat()
            if not lessor_name or not asset_description or redevance_amount <= 0 or duration_months <= 0:
                flash("Le bailleur, le bien, la redevance et la durée sont obligatoires.")
                return redirect(url_for('finance_lease_new'))

            c = cx()
            try:
                lease_id = create_finance_lease(
                    c, lessor_name, asset_description, asset_value, redevance_amount, periodicity,
                    start_date, duration_months, purchase_option_amount,
                    entity_id=current_entity_id(), notes=(request.form.get('notes') or '').strip(),
                    created_by=current_user()['email'],
                )
            except AccountingError as e:
                c.close()
                flash(f"Impossible de créer ce contrat : {e}")
                return redirect(url_for('finance_lease_new'))
            c.close()
            log_activity('FINANCE_LEASE_CREATED', f"Crédit-bail créé : {asset_description} ({lessor_name})")
            flash("Contrat de crédit-bail enregistré avec son échéancier de redevances.")
            return redirect(url_for('finance_lease_detail', lease_id=lease_id))

        return render_template('finance_lease_new.html', today=date.today().isoformat())

    @app.route('/comptabilite/credit-bail/<int:lease_id>')
    @login_required
    def finance_lease_detail(lease_id):
        c = cx()
        lease = c.execute('SELECT * FROM finance_leases WHERE id=?', (lease_id,)).fetchone()
        if not lease:
            c.close(); abort(404)
        payments = c.execute(
            'SELECT * FROM finance_lease_payments WHERE lease_id=? ORDER BY payment_number', (lease_id,)
        ).fetchall()
        remaining = remaining_commitment(c, lease_id)
        accounts = c.execute(
            "SELECT code,label FROM accounting_chart_of_accounts WHERE account_class=2 AND is_active=1 ORDER BY code"
        ).fetchall()
        c.close()
        return render_template('finance_lease_detail.html', lease=lease, payments=payments,
                                remaining=remaining, accounts=accounts, today=date.today().isoformat())

    @app.route('/comptabilite/credit-bail/redevance/<int:payment_id>/payer', methods=['POST'])
    @login_required
    def finance_lease_payment_pay(payment_id):
        from profitos.entities import current_entity_id
        c = cx()
        payment = c.execute('SELECT lease_id FROM finance_lease_payments WHERE id=?', (payment_id,)).fetchone()
        if not payment:
            c.close(); abort(404)
        try:
            pay_lease_payment(c, payment_id, entity_id=current_entity_id(), created_by=current_user()['email'])
        except AccountingError as e:
            c.close()
            flash(f"Règlement impossible : {e}")
            return redirect(url_for('finance_lease_detail', lease_id=payment['lease_id']))
        c.close()
        log_activity('FINANCE_LEASE_PAYMENT_PAID', f"Redevance de crédit-bail #{payment_id} réglée")
        flash("Redevance réglée, écriture comptable générée.")
        return redirect(url_for('finance_lease_detail', lease_id=payment['lease_id']))

    @app.route('/comptabilite/credit-bail/<int:lease_id>/lever-option', methods=['POST'])
    @login_required
    def finance_lease_exercise_option(lease_id):
        from profitos.entities import current_entity_id
        exercise_date = request.form.get('exercise_date') or date.today().isoformat()
        asset_account = request.form.get('asset_account')
        depreciation_account = request.form.get('depreciation_account')
        try:
            useful_life_years = float(request.form.get('useful_life_years') or 0)
        except ValueError:
            useful_life_years = 0
        if not asset_account or not depreciation_account or useful_life_years <= 0:
            flash("Le compte d'immobilisation, le compte d'amortissement et la durée sont obligatoires.")
            return redirect(url_for('finance_lease_detail', lease_id=lease_id))

        c = cx()
        try:
            asset_id = exercise_purchase_option(
                c, lease_id, exercise_date, useful_life_years,
                asset_account, depreciation_account, '681000',
                entity_id=current_entity_id(),
            )
        except AccountingError as e:
            c.close()
            flash(f"Levée d'option impossible : {e}")
            return redirect(url_for('finance_lease_detail', lease_id=lease_id))
        c.close()
        log_activity('FINANCE_LEASE_OPTION_EXERCISED', f"Option d'achat levée pour le crédit-bail #{lease_id}")
        flash("Option d'achat levée — le bien est maintenant une immobilisation à amortir.")
        return redirect(url_for('fixed_asset_detail', asset_id=asset_id))

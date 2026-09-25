import secrets

from profitos.runtime import *
from profitos.feature_access import requires_paid_plan
from profitos.gocardless_baas import (
    is_configured, create_redirect_flow, complete_redirect_flow, create_payment,
)


def register(app):
    @app.route('/facturation/<int:invoice_id>/prelevement', methods=['POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    @require_area('invoicing')
    def invoicing_gocardless_start(invoice_id):
        if not is_configured():
            flash("GoCardless n'est pas configuré côté serveur (GOCARDLESS_ACCESS_TOKEN manquant).")
            return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
        c = cx()
        inv = c.execute('SELECT * FROM outgoing_invoices WHERE id=?', (invoice_id,)).fetchone()
        if not inv:
            c.close(); abort(404)

        session_token = secrets.token_urlsafe(24)
        session['gc_session_token'] = session_token
        session['gc_invoice_id'] = invoice_id
        base = os.environ.get('APP_BASE_URL', request.host_url.rstrip('/'))
        redirect_uri = f"{base}{url_for('invoicing_gocardless_callback')}"

        try:
            flow = create_redirect_flow(
                inv['client_name'], inv['client_email'],
                f"Facture {inv['invoice_number']}", redirect_uri, session_token,
            )
        except ValueError as e:
            c.close()
            flash(f"Impossible de démarrer le prélèvement : {e}")
            return redirect(url_for('invoicing_detail', invoice_id=invoice_id))

        c.execute(
            """INSERT INTO gocardless_mandates(client_name,client_email,status,redirect_flow_id,authorization_url,created_at)
               VALUES(?,?,?,?,?,?)""",
            (inv['client_name'], inv['client_email'], 'pending', flow['id'], flow['authorization_url'], now()),
        )
        c.commit()
        mandate_row_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
        session['gc_mandate_row_id'] = mandate_row_id
        c.close()
        log_activity('GOCARDLESS_MANDATE_STARTED', f"Parcours de mandat démarré pour la facture {inv['invoice_number']}")
        if not flow.get('authorization_url'):
            flash("GoCardless n'a renvoyé aucune URL d'autorisation — la demande n'a probablement pas abouti.")
            return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
        return redirect(flow['authorization_url'])

    @app.route('/facturation/prelevement/retour')
    @login_required
    def invoicing_gocardless_callback():
        flow_id = request.args.get('redirect_flow_id')
        session_token = session.pop('gc_session_token', None)
        invoice_id = session.pop('gc_invoice_id', None)
        mandate_row_id = session.pop('gc_mandate_row_id', None)
        if not flow_id or not session_token or not mandate_row_id:
            flash("Retour de prélèvement invalide — recommence la demande depuis la facture.")
            return redirect(url_for('invoicing_list'))

        try:
            result = complete_redirect_flow(flow_id, session_token)
        except ValueError as e:
            flash(f"Impossible de finaliser le mandat : {e}")
            return redirect(url_for('invoicing_list'))

        c = cx()
        c.execute(
            "UPDATE gocardless_mandates SET status='active',gc_customer_id=?,gc_mandate_id=? WHERE id=?",
            (result.get('customer_id'), result.get('mandate_id'), mandate_row_id),
        )
        c.commit()
        log_activity('GOCARDLESS_MANDATE_ACTIVE', f"Mandat GoCardless #{mandate_row_id} activé")

        if invoice_id and result.get('mandate_id'):
            inv = c.execute('SELECT * FROM outgoing_invoices WHERE id=?', (invoice_id,)).fetchone()
            if inv:
                try:
                    payment = create_payment(
                        result['mandate_id'], round(inv['total'] * 100), 'EUR',
                        f"Facture {inv['invoice_number']}",
                    )
                    c.execute(
                        "INSERT INTO gocardless_payments(mandate_row_id,invoice_id,gc_payment_id,amount,status,created_at) VALUES(?,?,?,?,?,?)",
                        (mandate_row_id, invoice_id, payment.get('id'), inv['total'], payment.get('status') or 'pending', now()),
                    )
                    c.commit()
                    log_activity('GOCARDLESS_PAYMENT_CREATED', f"Prélèvement demandé pour la facture {inv['invoice_number']}")
                    flash(f"Mandat activé — prélèvement de {fr_number(inv['total'],2)} € demandé.")
                except ValueError as e:
                    flash(f"Mandat activé, mais la demande de prélèvement a échoué : {e}")
                c.close()
                return redirect(url_for('invoicing_detail', invoice_id=invoice_id))
        c.close()
        flash("Mandat de prélèvement activé.")
        return redirect(url_for('invoicing_list'))

"""Écran « E-mails non distribués » (v492) : adresses clients rejetées ou signalées comme
spam, encore utilisées par une facture non réglée, et correction en une fois."""
from profitos.runtime import *
from profitos import email_delivery as ED
from profitos.accounting import paris_today

EDIT_ROLES = ('OWNER', 'ADMIN', 'MEMBER', 'ACCOUNTANT')


def register(app):

    @app.route('/facturation/emails-non-distribues', methods=['GET', 'POST'])
    @login_required
    @requires_active_plan
    @require_area('invoicing')
    def email_failures():
        from profitos.entities import current_entity_id
        eid = current_entity_id()
        c = cx()
        try:
            if request.method == 'POST':
                if current_role() == 'LECTURE':
                    abort(403)
                old = (request.form.get('old') or '').strip()
                new = (request.form.get('new') or '').strip()
                listed = {f['recipient'] for f in ED.failing(c, eid, today=paris_today())}
                if ED.norm(old) not in listed:
                    flash("Adresse introuvable dans la liste des e-mails non distribués.")
                else:
                    try:
                        n_inv, n_cli = ED.correct_address(c, eid, old, new)
                    except ValueError as exc:
                        flash(str(exc))
                    else:
                        log_activity('EMAIL_ADDRESS_CORRECTED', f"Adresse client corrigée : {mask_email(old)} → {mask_email(new)} "
                                                                f"({n_inv} facture(s), {n_cli} fiche(s))")
                        flash(f"Adresse corrigée sur {n_inv} facture(s) non réglée(s) et {n_cli} fiche(s) client. "
                              "Renvoyez la facture depuis sa fiche pour que le client la reçoive.")
                return redirect(url_for('email_failures'))
            rows = ED.failing(c, eid, today=paris_today())
        finally:
            c.close()
        return render_template('email_failures.html', rows=rows, can_edit=current_role() != 'LECTURE')

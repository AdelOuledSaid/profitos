import json
import secrets

from profitos.runtime import *
from profitos.feature_access import requires_paid_plan
from profitos.ecommerce import (
    is_connected, get_connection, save_connection, fetch_shopify_orders, fetch_woocommerce_orders,
    normalize_shop_domain,
)

PLATFORM_LABELS = {'shopify': 'Shopify', 'woocommerce': 'WooCommerce'}


def register(app):
    @app.route('/integrations/ecommerce')
    @login_required
    @require_area('settings')
    def ecommerce_settings():
        c = cx()
        connections = {p: get_connection(c, p) for p in PLATFORM_LABELS}
        c.close()
        return render_template('ecommerce_settings.html', platforms=PLATFORM_LABELS, connections=connections)

    @app.route('/integrations/ecommerce/<platform>/connecter', methods=['POST'])
    @login_required
    @require_area('settings')
    def ecommerce_connect(platform):
        if platform not in PLATFORM_LABELS:
            abort(404)
        shop_domain = (request.form.get('shop_domain') or '').strip()
        cred1 = (request.form.get('credential_1') or '').strip()
        cred2 = (request.form.get('credential_2') or '').strip()
        if not shop_domain or not cred1 or (platform == 'woocommerce' and not cred2):
            flash("Tous les champs requis pour cette plateforme doivent être renseignés.")
            return redirect(url_for('ecommerce_settings'))
        try:
            shop_domain = normalize_shop_domain(shop_domain, platform)
        except ValueError as e:
            flash(str(e))
            return redirect(url_for('ecommerce_settings'))
        c = cx()
        save_connection(c, platform, shop_domain, cred1, cred2, current_user()['email'])
        c.close()
        log_activity('ECOMMERCE_CONNECTED', f"{PLATFORM_LABELS[platform]} connecté ({shop_domain})")
        flash(f"{PLATFORM_LABELS[platform]} connecté.")
        return redirect(url_for('ecommerce_settings'))

    @app.route('/integrations/ecommerce/<platform>/deconnecter', methods=['POST'])
    @login_required
    @require_area('settings')
    def ecommerce_disconnect(platform):
        if platform not in PLATFORM_LABELS:
            abort(404)
        c = cx()
        c.execute('DELETE FROM ecommerce_connections WHERE platform=?', (platform,))
        c.commit(); c.close()
        flash(f"{PLATFORM_LABELS[platform]} déconnecté.")
        return redirect(url_for('ecommerce_settings'))

    @app.route('/integrations/ecommerce/<platform>/synchroniser', methods=['POST'])
    @login_required
    @requires_active_plan
    @requires_paid_plan
    def ecommerce_sync(platform):
        from profitos.entities import current_entity_id
        if platform not in PLATFORM_LABELS:
            abort(404)
        c = cx()
        connection = get_connection(c, platform)
        if not connection:
            c.close()
            flash(f"{PLATFORM_LABELS[platform]} n'est pas connecté.")
            return redirect(url_for('ecommerce_settings'))

        try:
            if platform == 'shopify':
                orders = fetch_shopify_orders(connection['shop_domain'], connection['credential_1'])
            else:
                orders = fetch_woocommerce_orders(connection['shop_domain'], connection['credential_1'], connection['credential_2'])
        except ValueError as e:
            c.close()
            flash(f"Synchronisation échouée : {e}")
            return redirect(url_for('ecommerce_settings'))

        entity_id = current_entity_id()
        already = {
            r['external_order_id'] for r in c.execute(
                'SELECT external_order_id FROM ecommerce_imported_orders WHERE platform=? AND entity_id IS ?',
                (platform, entity_id),
            ).fetchall()
        }
        created = 0
        for o in orders:
            if o['external_id'] in already or not o['line_items']:
                continue
            subtotal = sum(li['qty'] * li['unit_price'] for li in o['line_items'])
            clean_lines = [
                {'label': li['label'], 'qty': li['qty'], 'unit_price': li['unit_price'],
                 'vat_rate': 0, 'line_total': round(li['qty'] * li['unit_price'], 2)}
                for li in o['line_items']
            ]
            seq = c.execute(
                "SELECT COUNT(*) n FROM outgoing_invoices WHERE entity_id IS ?",
                (entity_id,),
            ).fetchone()['n'] + 1
            invoice_number = f"FA-{platform.upper()}-{date.today().year}-{seq:04d}"
            token = secrets.token_urlsafe(20)
            c.execute(
                """INSERT INTO outgoing_invoices(invoice_number,client_name,client_email,issue_date,
                   line_items,subtotal,vat_amount,total,status,public_token,created_at,entity_id,notes)
                   VALUES(?,?,?,?,?,?,?,?,'draft',?,?,?,?)""",
                (invoice_number, o['customer_name'], o['customer_email'], o['order_date'] or date.today().isoformat(),
                 json.dumps(clean_lines, ensure_ascii=False), round(subtotal, 2), 0, round(subtotal, 2),
                 token, now(), entity_id, f"Importé depuis {PLATFORM_LABELS[platform]} — commande {o['order_number']}"),
            )
            c.commit()
            new_invoice_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
            ac = auth_cx()
            ac.execute('INSERT INTO outgoing_invoice_tokens(token,organization_id,invoice_local_id,created_at) VALUES(?,?,?,?)',
                (token, session['org_id'], new_invoice_id, now())); ac.commit(); ac.close()
            c.execute(
                'INSERT INTO ecommerce_imported_orders(platform,entity_id,external_order_id,invoice_id,imported_at) VALUES(?,?,?,?,?)',
                (platform, entity_id, o['external_id'], new_invoice_id, now()),
            )
            c.commit()
            created += 1
        c.close()
        log_activity('ECOMMERCE_SYNCED', f"{PLATFORM_LABELS[platform]} : {created} commande(s) importée(s)")
        flash(f"{created} commande(s) importée(s) en brouillon — la TVA n'est pas calculée automatiquement, à vérifier avant envoi.")
        return redirect(url_for('ecommerce_settings'))

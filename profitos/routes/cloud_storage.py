import secrets
import uuid

from profitos.runtime import *
from profitos.cloud_storage import (
    PROVIDERS, is_configured, build_authorize_url, exchange_code_for_token,
    list_folder_files, download_file,
)
from profitos.accounting import generate_purchase_entry, AccountingError


def register(app):
    @app.route('/integrations/cloud-storage')
    @login_required
    @require_area('settings')
    def cloud_storage_settings():
        c = cx()
        connections = {
            row['provider']: row
            for row in c.execute('SELECT * FROM cloud_storage_connections').fetchall()
        }
        c.close()
        return render_template('cloud_storage_settings.html', providers=PROVIDERS,
                                connections=connections, is_configured=is_configured)

    @app.route('/integrations/cloud-storage/<provider>/connect')
    @login_required
    @require_area('settings')
    def cloud_storage_connect(provider):
        if provider not in PROVIDERS:
            abort(404)
        if not is_configured(provider):
            flash(f"{PROVIDERS[provider]['label']} n'est pas configuré côté serveur "
                  f"(variables d'environnement manquantes).")
            return redirect(url_for('cloud_storage_settings'))
        state = secrets.token_urlsafe(24)
        session['cloud_storage_oauth_state'] = state
        session['cloud_storage_oauth_provider'] = provider
        base = os.environ.get('APP_BASE_URL', request.host_url.rstrip('/'))
        redirect_uri = f"{base}{url_for('cloud_storage_callback', provider=provider)}"
        return redirect(build_authorize_url(provider, redirect_uri, state))

    @app.route('/integrations/cloud-storage/<provider>/callback')
    @login_required
    @require_area('settings')
    def cloud_storage_callback(provider):
        if provider not in PROVIDERS:
            abort(404)
        error = request.args.get('error')
        if error:
            flash(f"Connexion {PROVIDERS[provider]['label']} refusée ou annulée ({error}).")
            return redirect(url_for('cloud_storage_settings'))
        state = request.args.get('state')
        expected_state = session.pop('cloud_storage_oauth_state', None)
        expected_provider = session.pop('cloud_storage_oauth_provider', None)
        if not state or state != expected_state or provider != expected_provider:
            flash("Réponse OAuth invalide (state incohérent) — reconnecte-toi.")
            return redirect(url_for('cloud_storage_settings'))
        code = request.args.get('code')
        if not code:
            flash("Aucun code d'autorisation reçu.")
            return redirect(url_for('cloud_storage_settings'))

        base = os.environ.get('APP_BASE_URL', request.host_url.rstrip('/'))
        redirect_uri = f"{base}{url_for('cloud_storage_callback', provider=provider)}"
        try:
            tokens = exchange_code_for_token(provider, code, redirect_uri)
        except ValueError as e:
            flash(f"Échec de connexion à {PROVIDERS[provider]['label']} : {e}")
            return redirect(url_for('cloud_storage_settings'))

        c = cx()
        c.execute(
            """INSERT INTO cloud_storage_connections
               (provider,access_token,refresh_token,token_expires_at,connected_at,connected_by,status)
               VALUES(?,?,?,?,?,?,'active')
               ON CONFLICT(provider) DO UPDATE SET
                 access_token=excluded.access_token,refresh_token=excluded.refresh_token,
                 token_expires_at=excluded.token_expires_at,connected_at=excluded.connected_at,
                 connected_by=excluded.connected_by,status='active'""",
            (provider, tokens['access_token'], tokens['refresh_token'], tokens['expires_at'],
             now(), current_user()['email']),
        )
        c.commit(); c.close()
        log_activity('CLOUD_STORAGE_CONNECTED', f"{PROVIDERS[provider]['label']} connecté")
        flash(f"{PROVIDERS[provider]['label']} connecté. Configure le dossier à surveiller ci-dessous.")
        return redirect(url_for('cloud_storage_settings'))

    @app.route('/integrations/cloud-storage/<provider>/deconnecter', methods=['POST'])
    @login_required
    @require_area('settings')
    def cloud_storage_disconnect(provider):
        if provider not in PROVIDERS:
            abort(404)
        c = cx()
        c.execute('DELETE FROM cloud_storage_connections WHERE provider=?', (provider,))
        c.commit(); c.close()
        log_activity('CLOUD_STORAGE_DISCONNECTED', f"{PROVIDERS[provider]['label']} déconnecté")
        flash(f"{PROVIDERS[provider]['label']} déconnecté.")
        return redirect(url_for('cloud_storage_settings'))

    @app.route('/integrations/cloud-storage/<provider>/dossier', methods=['POST'])
    @login_required
    @require_area('settings')
    def cloud_storage_set_folder(provider):
        if provider not in PROVIDERS:
            abort(404)
        folder_path = (request.form.get('folder_path') or '').strip()
        c = cx()
        c.execute('UPDATE cloud_storage_connections SET folder_path=? WHERE provider=?', (folder_path, provider))
        c.commit(); c.close()
        flash("Dossier enregistré.")
        return redirect(url_for('cloud_storage_settings'))

    @app.route('/integrations/cloud-storage/<provider>/synchroniser', methods=['POST'])
    @login_required
    @require_area('settings')
    def cloud_storage_sync(provider):
        if provider not in PROVIDERS:
            abort(404)
        c = cx()
        connection = c.execute('SELECT * FROM cloud_storage_connections WHERE provider=?', (provider,)).fetchone()
        if not connection:
            c.close()
            flash(f"{PROVIDERS[provider]['label']} n'est pas connecté.")
            return redirect(url_for('cloud_storage_settings'))

        from profitos.routes.invoicing import (
            _purchase_pdf_extract, _purchase_ai_extract, _purchase_pdf_dir, _PURCHASE_PDF_MAX_BYTES,
        )
        from pypdf.errors import PyPdfError

        try:
            files = list_folder_files(c, connection)
        except ValueError as e:
            c.execute('UPDATE cloud_storage_connections SET last_sync_error=? WHERE id=?', (str(e), connection['id']))
            c.commit(); c.close()
            flash(f"Synchronisation échouée : {e}")
            return redirect(url_for('cloud_storage_settings'))

        already_imported = {
            r['provider_file_id'] for r in c.execute(
                'SELECT provider_file_id FROM cloud_storage_imported_files WHERE connection_id=?', (connection['id'],)
            ).fetchall()
        }
        created, failed = [], []
        for f in files:
            if f['file_id'] in already_imported:
                continue
            try:
                data = download_file(c, connection, f['file_id'])
            except ValueError as e:
                failed.append(f"{f['name']} : {e}")
                continue
            if len(data) > _PURCHASE_PDF_MAX_BYTES:
                failed.append(f"{f['name']} : dépasse 5 Mo")
                continue
            if data.startswith(b"%PDF-"):
                mime, ext = 'application/pdf', '.pdf'
            elif data.startswith(b"\xff\xd8\xff"):
                mime, ext = 'image/jpeg', '.jpg'
            elif data.startswith(b"\x89PNG\r\n\x1a\n"):
                mime, ext = 'image/png', '.png'
            else:
                failed.append(f"{f['name']} : format non reconnu")
                continue

            pdf_dir = _purchase_pdf_dir()
            stored = uuid.uuid4().hex + ext
            (pdf_dir / stored).write_bytes(data)
            path = pdf_dir / stored
            try:
                if mime == 'application/pdf':
                    try:
                        detected = _purchase_pdf_extract(path)
                    except (ValueError, PyPdfError) as text_err:
                        if isinstance(text_err, ValueError) and "PDF sans texte exploitable" not in str(text_err):
                            raise
                        detected = _purchase_ai_extract(path.read_bytes(), mime)
                else:
                    detected = _purchase_ai_extract(path.read_bytes(), mime)
            except ValueError as e:
                path.unlink(missing_ok=True)
                failed.append(f"{f['name']} : {e}")
                continue

            c.execute(
                """INSERT INTO purchase_invoices
                   (supplier_name,invoice_number,issue_date,due_date,subtotal,vat_amount,total,
                    status,notes,created_at,document_path,category,validation_status)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (detected['supplier_name'], detected['invoice_number'],
                 detected['issue_date'] or None, detected['due_date'] or None,
                 detected['subtotal'], detected['vat_amount'], detected['total'],
                 'unpaid', f"Importé depuis {PROVIDERS[provider]['label']}", now(), stored, 'autre', 'pending'),
            )
            c.commit()
            new_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
            try:
                purchase_row = c.execute('SELECT * FROM purchase_invoices WHERE id=?', (new_id,)).fetchone()
                generate_purchase_entry(c, purchase_row)
            except AccountingError as e:
                log_ops_event('ACCOUNTING_ENTRY_FAILED', outcome='ERROR', detail=f"achat cloud {new_id}: {e}")
            c.execute(
                'INSERT INTO cloud_storage_imported_files(connection_id,provider_file_id,purchase_invoice_id,imported_at) VALUES(?,?,?,?)',
                (connection['id'], f['file_id'], new_id, now()),
            )
            c.commit()
            created.append(detected['invoice_number'])

        c.execute('UPDATE cloud_storage_connections SET last_sync_at=?,last_sync_error=? WHERE id=?',
                   (now(), None if not failed else '; '.join(failed[:3]), connection['id']))
        c.commit(); c.close()
        log_activity('CLOUD_STORAGE_SYNCED', f"{PROVIDERS[provider]['label']} : {len(created)} facture(s) importée(s)")
        flash(f"{len(created)} facture(s) importée(s), {len(failed)} échec(s).")
        return redirect(url_for('cloud_storage_settings'))

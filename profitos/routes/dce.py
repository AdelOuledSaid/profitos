from profitos.runtime import *



def register(app):
    @app.route('/dce/<int:opportunity_id>/upload',methods=['POST'])
    @login_required
    @requires_active_plan
    @require_area('grow')
    @rate_limit(20,3600)
    def dce_upload(opportunity_id):
        if not PHASE2_ENABLED: abort(404)  # Bid Intelligence désactivé en V0
        c=cx(); opp=c.execute("SELECT * FROM opportunities WHERE id=? AND type='GROW'",(opportunity_id,)).fetchone()
        if not opp: c.close(); abort(404)
        f=request.files.get('file')
        if not f or not f.filename:
            c.close(); flash('Choisis un document DCE.'); return redirect(url_for('detail',kind='GROW',item_id=opportunity_id))
        path=None
        try:
            path, original_name=save_upload(f,'dce',ALLOWED_DCE_EXTENSIONS)
            text=extract_document_text(path)
            analysis=analyze_dce_text(text,opp,profile())
            c.execute("INSERT INTO dce_documents(opportunity_id,filename,filetype,text_content,analysis_json,go_score,recommendation,created_at) VALUES(?,?,?,?,?,?,?,?)",
                      (opportunity_id,original_name,path.suffix.lower(),text[:500000],json.dumps(analysis,ensure_ascii=False),analysis['go_score'],analysis['recommendation'],now()))
            c.commit(); flash(f"DCE analysé : {analysis['recommendation']} · score {analysis['go_score']}/100")
        except Exception as e:
            flash(f'Analyse DCE impossible : {e}')
        finally:
            if path: cleanup_upload(path)
            c.close()
        return redirect(url_for('detail',kind='GROW',item_id=opportunity_id))

    @app.route('/service-worker.js')
    def service_worker():
        """Servi depuis la racine (pas /static/) pour que son scope couvre toute
        l'application — un service worker ne peut contrôler que son propre
        répertoire et les sous-répertoires, sauf en-tête Service-Worker-Allowed."""
        resp=app.send_static_file('service-worker.js')
        resp.headers['Content-Type']='application/javascript; charset=utf-8'
        resp.headers['Service-Worker-Allowed']='/'
        return resp


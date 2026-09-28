from profitos.runtime import *
from profitos.accounting import AccountingError, create_entry
import csv, hashlib, io, json
from datetime import date, datetime


def _norm(s):
    return str(s or '').strip()


def _amount(v):
    if v is None or v == '': return 0.0
    if isinstance(v, (int,float)): return round(float(v),2)
    return round(float(str(v).replace('\u202f','').replace(' ','').replace(',','.')),2)


def _parse_rows(raw, filename):
    ext=(filename.rsplit('.',1)[-1] if '.' in filename else '').lower()
    rows=[]
    if ext == 'csv':
        text=raw.decode('utf-8-sig')
        sample=text[:4096]
        try: dialect=csv.Sniffer().sniff(sample, delimiters=';,\t|')
        except csv.Error: dialect=csv.excel
        rows=list(csv.DictReader(io.StringIO(text), dialect=dialect))
    elif ext in ('xlsx','xlsm'):
        from openpyxl import load_workbook
        ws=load_workbook(io.BytesIO(raw), read_only=True, data_only=True).active
        vals=list(ws.iter_rows(values_only=True))
        if not vals: return []
        headers=[_norm(x) for x in vals[0]]
        rows=[dict(zip(headers,r)) for r in vals[1:] if any(x is not None for x in r)]
    else:
        raise ValueError('Format non pris en charge : utilisez CSV ou XLSX.')
    aliases={
      'account_code':['CompteNum','Compte','account_code','account','compte'],
      'label':['EcritureLib','Libelle','Libellé','label','libelle'],
      'debit':['Debit','Débit','debit'], 'credit':['Credit','Crédit','credit'],
      'auxiliary_name':['CompAuxLib','Auxiliaire','auxiliary_name']}
    out=[]
    for r in rows:
        def get(k):
            for a in aliases[k]:
                if a in r and r[a] not in (None,''): return r[a]
            return ''
        acc=_norm(get('account_code'))
        if not acc: continue
        out.append({'account_code':acc,'label':_norm(get('label')) or 'Paie',
                    'auxiliary_name':_norm(get('auxiliary_name')) or None,
                    'debit':_amount(get('debit')),'credit':_amount(get('credit'))})
    return out


def register(app):
    @app.route('/comptabilite/paie', methods=['GET','POST'])
    @login_required
    def payroll_imports_list():
        from profitos.entities import current_entity_id
        eid=current_entity_id(); c=cx(); error=None
        if request.method=='POST':
            provider=_norm(request.form.get('provider')).upper()
            period=_norm(request.form.get('period_label'))
            entry_date=_norm(request.form.get('entry_date')) or date.today().isoformat()
            f=request.files.get('file')
            if provider not in ('SILAE','PAYFIT') or not period or not f or not f.filename:
                error='Prestataire, période et fichier CSV/XLSX sont obligatoires.'
            else:
                raw=f.read(); digest=hashlib.sha256(raw).hexdigest()
                exists=c.execute('SELECT id FROM payroll_imports WHERE entity_id IS ? AND file_sha256=?',(eid,digest)).fetchone()
                if exists: error='Ce fichier de paie a déjà été importé pour cette entité.'
                else:
                    try:
                        lines=_parse_rows(raw,f.filename)
                        if len(lines)<2: raise ValueError('Le fichier doit contenir au moins deux lignes comptables exploitables.')
                        td=round(sum(x['debit'] for x in lines),2); tc=round(sum(x['credit'] for x in lines),2)
                        status='pending' if abs(td-tc)<=0.01 else 'invalid'
                        msg=None if status=='pending' else f'Écriture déséquilibrée : débit {td:.2f} / crédit {tc:.2f}.'
                        c.execute('INSERT INTO payroll_imports(entity_id,provider,period_label,entry_date,original_filename,file_sha256,status,total_debit,total_credit,lines_json,error_message,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                  (eid,provider,period,entry_date,f.filename,digest,status,td,tc,json.dumps(lines,ensure_ascii=False),msg,current_user()['email'],datetime.utcnow().isoformat()))
                        c.commit(); flash('Import de paie préparé. Vérifiez-le avant validation.')
                        c.close(); return redirect(url_for('payroll_imports_list'))
                    except Exception as exc: error=str(exc)
        rows=c.execute('SELECT * FROM payroll_imports WHERE entity_id IS ? ORDER BY id DESC',(eid,)).fetchall(); c.close()
        return render_template('payroll_imports.html',rows=rows,error=error,today=date.today().isoformat())

    @app.route('/comptabilite/paie/<int:import_id>/valider', methods=['POST'])
    @login_required
    def payroll_import_validate(import_id):
        from profitos.entities import current_entity_id
        eid=current_entity_id(); c=cx()
        row=c.execute("SELECT * FROM payroll_imports WHERE id=? AND entity_id IS ?",(import_id,eid)).fetchone()
        if not row or row['status']!='pending':
            c.close(); abort(404)
        try:
            if not c.execute("SELECT code FROM accounting_journals WHERE code='PA'").fetchone():
                c.execute("INSERT INTO accounting_journals(code,label,journal_type,is_default,created_at) VALUES('PA','Paie','OD',0,?)",(datetime.utcnow().isoformat(),))
            lines=json.loads(row['lines_json'])
            entry_id=create_entry(c,'PA',row['entry_date'],f"Paie {row['provider']} — {row['period_label']}",lines,
                                  source_type='payroll_import',source_id=row['id'],created_by=current_user()['email'],entity_id=eid,commit=False)
            c.execute("UPDATE payroll_imports SET status='posted',accounting_entry_id=?,validated_by=?,validated_at=? WHERE id=? AND entity_id IS ? AND status='pending'",
                      (entry_id,current_user()['email'],datetime.utcnow().isoformat(),import_id,eid)); c.commit()
            log_activity('PAYROLL_IMPORT_POSTED',f"Import paie #{import_id} comptabilisé dans PA")
            flash('OD de paie validée et comptabilisée dans le journal PA.')
        except AccountingError as exc:
            c.rollback()
            flash(f'Validation impossible : {exc}')
        finally: c.close()
        return redirect(url_for('payroll_imports_list'))

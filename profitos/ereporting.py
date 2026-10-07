from collections import defaultdict
import hashlib, json

VAT_RATES={0,0.9,1.05,1.75,2.1,5.5,7,8.5,9.2,9.6,10,13,19.6,20,20.6}
CATEGORIES={'TLB1','TPS1','TNT1','TMA1'}
def _money(v): return round(float(v or 0),2)
def _vat_buckets(invoice):
    out=defaultdict(lambda:[0.0,0.0])
    for line in json.loads(invoice['line_items'] or '[]'):
        rate=float(line.get('vat_rate') or 0)
        if rate not in VAT_RATES: raise ValueError(f"Taux TVA e-reporting non admis: {rate:g}")
        net=_money(line.get('line_total')); vat=round(net*rate/100,2)
        out[rate][0]+=net; out[rate][1]+=vat
    return {r:(round(x[0],2),round(x[1],2)) for r,x in out.items()}
def _payment_split(invoice,amount):
    b=_vat_buckets(invoice); gross={r:round(n+v,2) for r,(n,v) in b.items()}; total=sum(gross.values())
    if total<=0:return []
    rem=_money(amount); ans=[]; items=list(gross.items())
    for i,(rate,g) in enumerate(items):
        part=rem if i==len(items)-1 else round(_money(amount)*g/total,2); rem=round(rem-part,2); ans.append((rate,part))
    return ans
def _tx_number(entity_id,flow_type,anchor_date,ids):
    raw=f"{entity_id or 0}|{flow_type}|{anchor_date}|"+",".join(map(str,sorted(ids)))
    return "PO-"+hashlib.sha256(raw.encode()).hexdigest()[:28].upper()

def build_flux10(conn,entity_id,anchor_date,flow_type):
    if flow_type=='AggregatedCustomerTransactionReport':
        rows=conn.execute("""SELECT * FROM outgoing_invoices WHERE entity_id IS ?
          AND ereporting_scope='b2c' AND issue_date=? AND status!='draft' ORDER BY id""",(entity_id,anchor_date)).fetchall()
        agg=defaultdict(lambda:[0,0.0,0.0])
        for x in rows:
            cat=x['ereporting_category']
            if cat not in CATEGORIES: raise ValueError(f"Catégorie e-reporting absente/invalide pour {x['invoice_number']}")
            k=(x['issue_date'],cat); agg[k][0]+=1; agg[k][1]+=_money(x['subtotal']); agg[k][2]+=_money(x['vat_amount'])
        lines=[{'activityDate':d,'currency':'EUR','categoryCode':cat,'operationCount':v[0],
                'netAmount':round(v[1],2),'vatAmount':round(v[2],2)} for (d,cat),v in agg.items()]
    elif flow_type=='UnitaryCustomerTransactionReport':
        rows=conn.execute("""SELECT * FROM outgoing_invoices WHERE entity_id IS ?
          AND ereporting_scope='international_b2b' AND issue_date=? AND status!='draft' ORDER BY id""",(entity_id,anchor_date)).fetchall()
        lines=[]
        for x in rows:
            country=(x['counterparty_country'] or '').upper()
            if len(country)!=2 or country=='FR': raise ValueError(f"Pays étranger requis pour {x['invoice_number']}")
            q={'invoiceNumber':x['invoice_number'],'invoiceDate':x['issue_date'],'counterpartCountry':country,
               'netAmount':_money(x['subtotal']),'vatAmount':_money(x['vat_amount']),'currency':'EUR'}
            if (x['client_vat_number'] or '').strip(): q['counterpartVatId']=x['client_vat_number'].strip()
            lines.append(q)
    elif flow_type in ('AggregatedCustomerPaymentReport','UnitaryCustomerPaymentReport'):
        scope='b2c' if flow_type=='AggregatedCustomerPaymentReport' else 'international_b2b'
        rows=conn.execute("""SELECT p.*,i.invoice_number,i.issue_date,i.line_items,i.counterparty_country
          FROM outgoing_invoice_payments p JOIN outgoing_invoices i ON i.id=p.invoice_id AND COALESCE(CAST(i.entity_id AS TEXT),'') = COALESCE(CAST(p.entity_id AS TEXT),'')
          WHERE p.entity_id IS ? AND i.ereporting_scope=? AND p.payment_date=? ORDER BY p.id""",(entity_id,scope,anchor_date)).fetchall()
        if scope=='b2c':
            agg=defaultdict(lambda:[0,0.0])
            for x in rows:
                for rate,amount in _payment_split(x,x['amount']):
                    agg[(x['payment_date'],rate)][0]+=1; agg[(x['payment_date'],rate)][1]+=amount
            lines=[{'paymentDate':d,'currency':'EUR','vatRate':rate,'operationCount':v[0],'amount':round(v[1],2)}
                   for (d,rate),v in agg.items()]
        else:
            lines=[]
            for x in rows:
                country=(x['counterparty_country'] or '').upper()
                if len(country)!=2 or country=='FR': raise ValueError(f"Pays étranger requis pour {x['invoice_number']}")
                for rate,amount in _payment_split(x,x['amount']):
                    lines.append({'invoiceNumber':x['invoice_number'],'invoiceDate':x['issue_date'],
                                  'paymentDate':x['payment_date'],'amount':amount,'vatRate':rate,'currency':'EUR'})
    else: raise ValueError("Type de flux F4B non pris en charge")
    if not lines:return None
    ids=[x['id'] for x in rows]
    line_key={
        'AggregatedCustomerTransactionReport':'dailyTransactions',
        'UnitaryCustomerTransactionReport':'invoiceTransactions',
        'AggregatedCustomerPaymentReport':'dailyPayments',
        'UnitaryCustomerPaymentReport':'invoicePayments',
    }[flow_type]
    return {'flowType':flow_type,'transmissionNumber':_tx_number(entity_id,flow_type,anchor_date,ids),
            'anchorDate':anchor_date,'typeCode':'IN','lines':{line_key:lines}}

from .store import one, many

SCHEMA = '''
CREATE TABLE IF NOT EXISTS credit_claims(id TEXT PRIMARY KEY,case_id TEXT,tenant TEXT,period TEXT,amount INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS settlement_receipts(id TEXT PRIMARY KEY,claim_id TEXT,amount INTEGER,actor TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS settlement_reversals(id TEXT PRIMARY KEY,receipt_id TEXT,amount INTEGER,reason TEXT);
CREATE TABLE IF NOT EXISTS credit_claim_lines(id TEXT PRIMARY KEY,claim_id TEXT,request_uid TEXT,amount INTEGER,status TEXT);
'''


def _case_window(db, case):
    return one(db, 'SELECT * FROM windows WHERE id=?', (case['window_id'],))


def _current_uids(db, window):
    """UIDs whose failure still has business support at this moment.

    ``certain`` are provable failures (eligible for reserving credit); the
    wider ``supported`` set also contains UIDs that are only possibly failing.
    """
    from .cause import _evaluate
    objective = one(db, 'SELECT * FROM objectives WHERE id=?', (window['objective'],))
    evidence, waivers, supported, certain = _evaluate(db, window, objective)
    return certain, evidence, waivers


def handle(db,x):
    op=x['op']
    if op=='claim':
        case=one(db,'SELECT * FROM cause_cases WHERE id=?',(x['case'],))
        if not case or case['status']!='decided' or case['liable']=='waived' or x['amount']<=0 or x['amount']>x['cap']:
            return {'decision':'claim_denied'}
        competing=one(db,"SELECT id FROM credit_claims WHERE case_id=? AND tenant=? AND period=? AND status!='reversed'",(x['case'],x['tenant'],x['period']))
        if competing:return {'decision':'claim_already_reserved'}
        lines=x.get('lines')
        if lines:
            window=_case_window(db,case)
            supported,_,_=_current_uids(db,window)
            uids=[line.get('request_uid') for line in lines]
            if not uids or len(uids)!=len(set(uids)) or any(uid not in supported for uid in uids):
                return {'decision':'claim_denied'}
            amounts=[line.get('amount',0) for line in lines]
            if any(amount<=0 for amount in amounts) or sum(amounts)!=x['amount']:
                return {'decision':'claim_denied'}
        db.execute('INSERT INTO credit_claims VALUES(?,?,?,?,?,?)',(x['id'],x['case'],x['tenant'],x['period'],x['amount'],'draft'))
        if lines:
            for index,line in enumerate(lines):
                line_id=f"{x['id']}-L{index+1}"
                db.execute('INSERT INTO credit_claim_lines VALUES(?,?,?,?,?)',(line_id,x['id'],line['request_uid'],line['amount'],'supported'))
        return {'decision':'claim_reserved','liable':case['liable']}
    if op=='settle':
        claim=one(db,'SELECT * FROM credit_claims WHERE id=?',(x['claim'],))
        if not claim or claim['status']!='draft' or x['amount']!=claim['amount'] or not x.get('actor'):
            return {'decision':'settlement_denied'}
        case=one(db,'SELECT * FROM cause_cases WHERE id=?',(claim['case_id'],))
        topo=one(db,'SELECT * FROM topology WHERE id=?',(case['topology_id'],)) if case else None
        if not case or case['status']!='decided' or not topo or topo['generation']!=case['topology_generation']:
            return {'decision':'source_changed'}
        lines=many(db,'SELECT * FROM credit_claim_lines WHERE claim_id=?',(x['claim'],))
        if any(line['status']=='frozen' for line in lines):
            return {'decision':'settlement_denied'}
        db.execute('INSERT INTO settlement_receipts VALUES(?,?,?,?,?)',(x['id'],x['claim'],x['amount'],x['actor'],'signed'))
        db.execute("UPDATE credit_claims SET status='settled' WHERE id=?",(x['claim'],))
        return {'decision':'settled'}
    if op=='reverse':
        receipt=one(db,'SELECT * FROM settlement_receipts WHERE id=?',(x['receipt'],))
        if not receipt or receipt['status']!='signed' or not x.get('reason'):
            return {'decision':'reversal_denied'}
        db.execute('INSERT INTO settlement_reversals VALUES(?,?,?,?)',(x['id'],x['receipt'],receipt['amount'],x['reason']))
        db.execute("UPDATE settlement_receipts SET status='reversed' WHERE id=?",(x['receipt'],))
        db.execute("UPDATE credit_claims SET status='reversed' WHERE id=?",(receipt['claim_id'],))
        return {'decision':'reversed'}
    if op=='split_period':
        case=one(db,'SELECT * FROM cause_cases WHERE id=?',(x['case'],))
        claims=many(db,'SELECT * FROM credit_claims WHERE case_id=?',(x['case'],))
        if not case or case['status']!='decided' or len({c['period'] for c in claims})<2:
            return {'decision':'not_cross_period'}
        return {'decision':'split_confirmed','periods':sorted({c['period'] for c in claims})}
    return {'decision':'unknown_operation'}

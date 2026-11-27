"""FX lock commitments for synthetic compensation settlement.

A compensation claim is booked in one currency while the customer is paid in
another.  Quotes publish an integer conversion ratio (rate_num / rate_den), an
effective window and a face amount in the contract currency.  A claim may take
soft holds (uncommitted occupancy) or firm locks (a lock commitment); several
receipts can draw down one lock.  Whole-order contracts demand a group lock that
covers the full claim and only fulfils when every leg is externally confirmed.
"""
from .store import one, many

SCHEMA = '''
CREATE TABLE IF NOT EXISTS fx_quotes(id TEXT PRIMARY KEY,pair TEXT,base_ccy TEXT,quote_ccy TEXT,rate_num INTEGER,rate_den INTEGER,start_at INTEGER,end_at INTEGER,face INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS fx_holds(id TEXT PRIMARY KEY,quote_id TEXT,claim_id TEXT,face INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS fx_locks(id TEXT PRIMARY KEY,quote_id TEXT,claim_id TEXT,face INTEGER,group_id TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS fx_groups(id TEXT PRIMARY KEY,claim_id TEXT,required_face INTEGER,mode TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS fx_receipts(id TEXT PRIMARY KEY,lock_id TEXT,quote_id TEXT,claim_id TEXT,face INTEGER,received_ccy TEXT,received_amount INTEGER,fee INTEGER,rate_num INTEGER,rate_den INTEGER,ext_ref TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS fx_reversals(id TEXT PRIMARY KEY,receipt_id TEXT,face INTEGER,received_amount INTEGER,fee INTEGER,reason TEXT);
'''


def _pair(pair):
    if not isinstance(pair, str) or pair.count('/') != 1:
        return None, None
    base, quoted = pair.split('/')
    if not base or not quoted:
        return None, None
    return base, quoted


def _available(db, quote_id):
    """Face still bookable: quote face minus soft holds and firm locks.

    Suspended locks remain bound: a paused commitment never releases its quota.
    """
    quote = one(db, 'SELECT * FROM fx_quotes WHERE id=?', (quote_id,))
    if not quote:
        return None, 0
    held = one(db, "SELECT COALESCE(SUM(face),0) n FROM fx_holds WHERE quote_id=? AND status='held'", (quote_id,))['n']
    locked = one(db, "SELECT COALESCE(SUM(face),0) n FROM fx_locks WHERE quote_id=? AND status IN ('held','suspended')", (quote_id,))['n']
    return quote, quote['face'] - held - locked


def _draft_claim(db, claim_id):
    claim = one(db, 'SELECT * FROM credit_claims WHERE id=?', (claim_id,))
    if not claim or claim['status'] != 'draft':
        return None
    return claim


def _source_fresh(db, claim):
    case = one(db, 'SELECT * FROM cause_cases WHERE id=?', (claim['case_id'],))
    topo = one(db, 'SELECT * FROM topology WHERE id=?', (case['topology_id'],)) if case else None
    return bool(case and case['status'] == 'decided' and topo and topo['generation'] == case['topology_generation'])


def _lock_consumed(db, lock_id):
    return one(db, "SELECT COALESCE(SUM(face),0) n FROM fx_receipts WHERE lock_id=? AND status!='reversed'", (lock_id,))['n']


def _claim_received_face(db, claim_id):
    return one(db, "SELECT COALESCE(SUM(face),0) n FROM fx_receipts WHERE claim_id=? AND status!='reversed'", (claim_id,))['n']


def _segmented_settled(db, claim):
    """Segmented contracts settle once signed receipts cover the whole claim."""
    if any(lock['group_id'] for lock in many(db, 'SELECT * FROM fx_locks WHERE claim_id=?', (claim['id'],))):
        return False
    return _claim_received_face(db, claim['id']) >= claim['amount']


def _group_complete(db, group):
    legs = many(db, 'SELECT * FROM fx_locks WHERE group_id=?', (group['id'],))
    for leg in legs:
        if not one(db, "SELECT 1 FROM fx_receipts WHERE lock_id=? AND status='confirmed' LIMIT 1", (leg['id'],)):
            return False
    confirmed_face = one(db, "SELECT COALESCE(SUM(r.face),0) n FROM fx_receipts r JOIN fx_locks l ON l.id=r.lock_id WHERE l.group_id=? AND r.status='confirmed'", (group['id'],))['n']
    return confirmed_face >= group['required_face']


def freeze_claims(db, claim_ids):
    """Pause unperformed FX occupancy for frozen claims.

    Firm locks are suspended but keep their quote quota bound; soft holds were
    never committed, so their quota is released back to the quote.
    """
    if not claim_ids:
        return
    marks = ','.join('?' for _ in claim_ids)
    db.execute(f"UPDATE fx_holds SET status='released' WHERE status='held' AND claim_id IN ({marks})", tuple(claim_ids))
    db.execute(f"UPDATE fx_locks SET status='suspended' WHERE status='held' AND claim_id IN ({marks})", tuple(claim_ids))


def resume_claims(db, claim_ids):
    """Re-open suspended lock commitments after the underlying case revives."""
    if not claim_ids:
        return
    marks = ','.join('?' for _ in claim_ids)
    db.execute(f"UPDATE fx_locks SET status='held' WHERE status='suspended' AND claim_id IN ({marks})", tuple(claim_ids))


def handle(db, x):
    op = x['op']
    if op == 'quote':
        base, quoted = _pair(x.get('pair', ''))
        if not base or x.get('rate_num', 0) <= 0 or x.get('rate_den', 0) <= 0 or x.get('face', 0) <= 0 or x['end'] <= x['start']:
            return {'decision': 'invalid_quote'}
        if one(db, 'SELECT 1 FROM fx_quotes WHERE id=?', (x['id'],)):
            return {'decision': 'quote_identity_conflict'}
        db.execute('INSERT INTO fx_quotes VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (x['id'], x['pair'], base, quoted, x['rate_num'], x['rate_den'], x['start'], x['end'], x['face'], 'active'))
        return {'decision': 'quote_active', 'id': x['id'], 'pair': x['pair'], 'available': x['face']}

    if op == 'hold':
        quote, available = _available(db, x['quote'])
        if not quote:
            return {'decision': 'fx_quote_not_found'}
        if not _draft_claim(db, x['claim']):
            return {'decision': 'fx_claim_unavailable'}
        if x.get('face', 0) <= 0 or x['face'] > available:
            return {'decision': 'fx_quote_exhausted', 'available': available}
        db.execute('INSERT INTO fx_holds VALUES(?,?,?,?,?)', (x['id'], x['quote'], x['claim'], x['face'], 'held'))
        return {'decision': 'fx_held', 'id': x['id'], 'available': available - x['face']}

    if op == 'release':
        hold = one(db, "SELECT * FROM fx_holds WHERE id=? AND status='held'", (x['id'],))
        if not hold:
            return {'decision': 'fx_hold_unavailable'}
        db.execute("UPDATE fx_holds SET status='released' WHERE id=?", (x['id'],))
        return {'decision': 'fx_hold_released', 'id': x['id']}

    if op == 'lock':
        quote, available = _available(db, x['quote'])
        if not quote:
            return {'decision': 'fx_quote_not_found'}
        claim = _draft_claim(db, x['claim'])
        if not claim:
            return {'decision': 'fx_claim_unavailable'}
        if x.get('face', 0) <= 0:
            return {'decision': 'fx_quote_exhausted', 'available': available}
        if x['face'] > claim['amount']:
            return {'decision': 'fx_lock_exceeds_claim'}
        if x['face'] > available:
            return {'decision': 'fx_quote_exhausted', 'available': available}
        db.execute('INSERT INTO fx_locks VALUES(?,?,?,?,?,?)', (x['id'], x['quote'], x['claim'], x['face'], None, 'held'))
        return {'decision': 'fx_locked', 'id': x['id'], 'quote': x['quote'], 'face': x['face'], 'available': available - x['face']}

    if op == 'group_lock':
        claim = _draft_claim(db, x['claim'])
        if not claim:
            return {'decision': 'fx_claim_unavailable'}
        legs = x.get('legs') or []
        if not legs or any(not leg.get('quote') or leg.get('face', 0) <= 0 for leg in legs):
            return {'decision': 'invalid_group_lock'}
        needed = {}
        for leg in legs:
            quote, available = _available(db, leg['quote'])
            if not quote:
                return {'decision': 'fx_quote_not_found', 'quote': leg['quote']}
            needed[leg['quote']] = needed.get(leg['quote'], 0) + leg['face']
            if needed[leg['quote']] > available:
                return {'decision': 'fx_quote_exhausted', 'quote': leg['quote'], 'available': available}
        total = sum(leg['face'] for leg in legs)
        if total < claim['amount']:
            return {'decision': 'fx_group_undercovered', 'covered': total, 'required': claim['amount']}
        db.execute('INSERT INTO fx_groups VALUES(?,?,?,?,?)', (x['id'], x['claim'], claim['amount'], 'whole', 'open'))
        for idx, leg in enumerate(legs):
            db.execute('INSERT INTO fx_locks VALUES(?,?,?,?,?,?)',
                       (f"{x['id']}-leg{idx}", leg['quote'], x['claim'], leg['face'], x['id'], 'held'))
        return {'decision': 'fx_group_locked', 'id': x['id'], 'legs': len(legs), 'face': total, 'status': 'open'}

    if op == 'receipt':
        lock = one(db, 'SELECT * FROM fx_locks WHERE id=?', (x['lock'],))
        if not lock:
            return {'decision': 'fx_lock_unavailable'}
        if lock['status'] == 'suspended':
            return {'decision': 'fx_lock_suspended'}
        if lock['status'] != 'held':
            return {'decision': 'fx_lock_unavailable'}
        claim = one(db, 'SELECT * FROM credit_claims WHERE id=?', (lock['claim_id'],))
        if not claim or claim['status'] != 'draft':
            return {'decision': 'fx_claim_unavailable'}
        if not _source_fresh(db, claim):
            return {'decision': 'source_changed'}
        quote = one(db, 'SELECT * FROM fx_quotes WHERE id=?', (lock['quote_id'],))
        if not quote or quote['status'] != 'active' or not quote['start_at'] <= x['at'] < quote['end_at']:
            return {'decision': 'quote_window_closed'}
        face = x.get('amount', 0)
        fee = x.get('fee', 0)
        if face <= 0 or fee < 0:
            return {'decision': 'invalid_receipt'}
        if _lock_consumed(db, lock['id']) + face > lock['face']:
            return {'decision': 'fx_lock_exhausted', 'available': lock['face'] - _lock_consumed(db, lock['id'])}
        gross = face * quote['rate_num'] // quote['rate_den']
        received = gross - fee
        if received < 0:
            return {'decision': 'fx_fee_exceeds_proceeds', 'gross': gross}
        db.execute('INSERT INTO fx_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (x['id'], lock['id'], quote['id'], claim['id'], face, quote['quote_ccy'],
                    received, fee, quote['rate_num'], quote['rate_den'], x.get('ext_ref'), 'signed'))
        decision = 'fx_receipt_signed'
        if _segmented_settled(db, claim):
            db.execute("UPDATE credit_claims SET status='settled' WHERE id=?", (claim['id'],))
            decision = 'fx_claim_settled'
        return {'decision': decision, 'receipt': x['id'], 'pair': quote['pair'],
                'received_ccy': quote['quote_ccy'], 'received_amount': received, 'fee': fee,
                'rate': f"{quote['rate_num']}/{quote['rate_den']}",
                'lock_remaining': lock['face'] - _lock_consumed(db, lock['id']),
                'claim_received_face': _claim_received_face(db, claim['id']), 'claim_amount': claim['amount']}

    if op == 'confirm':
        receipt = one(db, "SELECT * FROM fx_receipts WHERE id=? AND status!='reversed'", (x['receipt'],))
        if not receipt:
            return {'decision': 'fx_receipt_unavailable'}
        if receipt['status'] == 'confirmed':
            return {'decision': 'fx_receipt_confirmed', 'receipt': receipt['id'], 'idempotent': True}
        db.execute("UPDATE fx_receipts SET status='confirmed', ext_ref=COALESCE(?, ext_ref) WHERE id=?",
                   (x.get('ext_ref'), receipt['id']))
        lock = one(db, 'SELECT * FROM fx_locks WHERE id=?', (receipt['lock_id'],))
        if not lock['group_id']:
            return {'decision': 'fx_receipt_confirmed', 'receipt': receipt['id']}
        group = one(db, 'SELECT * FROM fx_groups WHERE id=?', (lock['group_id'],))
        confirmed_face = one(db, "SELECT COALESCE(SUM(r.face),0) n FROM fx_receipts r JOIN fx_locks l ON l.id=r.lock_id WHERE l.group_id=? AND r.status='confirmed'", (group['id'],))['n']
        if group['status'] == 'open' and _group_complete(db, group):
            db.execute("UPDATE fx_groups SET status='fulfilled' WHERE id=?", (group['id'],))
            db.execute("UPDATE credit_claims SET status='settled' WHERE id=?", (group['claim_id'],))
            return {'decision': 'fx_delivery_fulfilled', 'group': group['id'], 'confirmed_face': confirmed_face}
        # A partial external confirmation is a fact: quota stays consumed and
        # the whole-order contract must not be declared performed.
        return {'decision': 'fx_delivery_pending', 'group': group['id'], 'confirmed_face': confirmed_face,
                'required': group['required_face'], 'group_status': group['status']}

    if op == 'reverse':
        receipt = one(db, "SELECT * FROM fx_receipts WHERE id=? AND status!='reversed'", (x['receipt'],))
        if not receipt or not x.get('reason'):
            return {'decision': 'fx_reversal_denied'}
        db.execute('INSERT INTO fx_reversals VALUES(?,?,?,?,?,?)',
                   (x['id'], receipt['id'], receipt['face'], receipt['received_amount'], receipt['fee'], x['reason']))
        db.execute("UPDATE fx_receipts SET status='reversed' WHERE id=?", (receipt['id'],))
        claim = one(db, 'SELECT * FROM credit_claims WHERE id=?', (receipt['claim_id'],))
        if claim and claim['status'] == 'settled':
            db.execute("UPDATE credit_claims SET status='draft' WHERE id=?", (claim['id'],))
        lock = one(db, 'SELECT * FROM fx_locks WHERE id=?', (receipt['lock_id'],))
        if lock and lock['group_id']:
            group = one(db, "SELECT * FROM fx_groups WHERE id=? AND status='fulfilled'", (lock['group_id'],))
            if group:
                db.execute("UPDATE fx_groups SET status='open' WHERE id=?", (group['id'],))
        return {'decision': 'fx_reversed', 'receipt': receipt['id'],
                'restored_face': receipt['face'], 'restored_received': receipt['received_amount'],
                'non_refundable_fee': receipt['fee'], 'revalued': False,
                'rate': f"{receipt['rate_num']}/{receipt['rate_den']}"}

    if op == 'position':
        claim = one(db, 'SELECT * FROM credit_claims WHERE id=?', (x['claim'],))
        if not claim:
            return {'decision': 'fx_claim_unavailable'}
        receipts = many(db, "SELECT * FROM fx_receipts WHERE claim_id=? AND status!='reversed'", (x['claim'],))
        by_ccy = {}
        for r in receipts:
            bucket = by_ccy.setdefault(r['received_ccy'], {'received': 0, 'fee': 0, 'receipts': 0})
            bucket['received'] += r['received_amount']
            bucket['fee'] += r['fee']
            bucket['receipts'] += 1
        received_face = sum(r['face'] for r in receipts)
        return {'decision': 'fx_position', 'claim': x['claim'], 'claim_status': claim['status'],
                'claim_amount': claim['amount'], 'received_face': received_face,
                'outstanding_face': max(0, claim['amount'] - received_face), 'by_ccy': by_ccy}
    return {'decision': 'unknown_operation'}

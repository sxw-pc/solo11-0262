import json

from .store import one, many
from .cause import liable_uid_detail

SCHEMA = '''
CREATE TABLE IF NOT EXISTS credit_claims(id TEXT PRIMARY KEY,case_id TEXT,tenant TEXT,period TEXT,amount INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS settlement_receipts(id TEXT PRIMARY KEY,claim_id TEXT,amount INTEGER,actor TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS settlement_reversals(id TEXT PRIMARY KEY,receipt_id TEXT,amount INTEGER,reason TEXT);
CREATE TABLE IF NOT EXISTS apportion_plans(id TEXT PRIMARY KEY,case_id TEXT,tenant TEXT,total INTEGER,unit INTEGER,contract TEXT,status TEXT,created_by TEXT);
CREATE TABLE IF NOT EXISTS apportion_periods(plan_id TEXT,period TEXT,case_id TEXT,cap INTEGER,uids TEXT,supported INTEGER,authority INTEGER,claim_id TEXT,extra_claims TEXT,closed INTEGER,PRIMARY KEY(plan_id,period));
CREATE TABLE IF NOT EXISTS apportion_uids(plan_id TEXT,uid TEXT,weight INTEGER,lots INTEGER,amount INTEGER,home_period TEXT,state TEXT,PRIMARY KEY(plan_id,uid));
CREATE TABLE IF NOT EXISTS apportion_balances(plan_id TEXT,name TEXT,amount INTEGER,PRIMARY KEY(plan_id,name));
CREATE TABLE IF NOT EXISTS uid_attributions(id TEXT PRIMARY KEY,plan_id TEXT,uid TEXT,period TEXT,actor TEXT,claim_id TEXT,amount INTEGER,routing TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS apportion_adjustments(id TEXT PRIMARY KEY,plan_id TEXT,kind TEXT,amount INTEGER,detail TEXT,reason TEXT);
'''

BALANCE_NAMES = ('dust', 'unattributed', 'cap_blocked')
CONTRACT_ALIASES = {
    'next_bill': 'next_bill', 'next': 'next_bill', 'forward': 'next_bill',
    'carry_forward': 'next_bill', 'next_period': 'next_bill',
    'return_period': 'return_period', 'return': 'return_period',
    'back': 'return_period', 'original_period': 'return_period', 'catch_up': 'return_period',
}


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _err(decision, **extra):
    result = {'decision': decision}
    result.update(extra)
    return result


def handle(db, x):
    op = x['op']
    if op == 'claim':
        case = one(db, 'SELECT * FROM cause_cases WHERE id=?', (x['case'],))
        cap = x.get('cap')
        if not case or case['status'] != 'decided' or case['liable'] == 'waived' or x['amount'] <= 0 or (cap is not None and x['amount'] > cap):
            return {'decision': 'claim_denied'}
        competing = one(db, "SELECT id FROM credit_claims WHERE case_id=? AND tenant=? AND period=? AND status!='reversed'", (x['case'], x['tenant'], x['period']))
        if competing:
            return {'decision': 'claim_already_reserved'}
        plan_period = _match_plan_period(db, x['case'], x['period'])
        if plan_period:
            booked = one(db, "SELECT COALESCE(SUM(amount),0) n FROM credit_claims WHERE case_id=? AND tenant=? AND period=? AND status!='reversed'",
                         (x['case'], x['tenant'], x['period']))['n']
            if booked + x['amount'] > plan_period['authority']:
                return {'decision': 'claim_denied'}
        db.execute('INSERT INTO credit_claims VALUES(?,?,?,?,?,?)', (x['id'], x['case'], x['tenant'], x['period'], x['amount'], 'draft'))
        if plan_period and not plan_period['claim_id']:
            db.execute('UPDATE apportion_periods SET claim_id=? WHERE plan_id=? AND period=?',
                       (x['id'], plan_period['plan_id'], x['period']))
        return {'decision': 'claim_reserved', 'liable': case['liable']}
    if op == 'settle':
        claim = one(db, 'SELECT * FROM credit_claims WHERE id=?', (x['claim'],))
        if not claim or claim['status'] != 'draft' or x['amount'] != claim['amount'] or not x.get('actor'):
            return {'decision': 'settlement_denied'}
        case = one(db, 'SELECT * FROM cause_cases WHERE id=?', (claim['case_id'],))
        topo = one(db, 'SELECT * FROM topology WHERE id=?', (case['topology_id'],)) if case else None
        if not case or case['status'] != 'decided' or not topo or topo['generation'] != case['topology_generation']:
            return {'decision': 'source_changed'}
        db.execute('INSERT INTO settlement_receipts VALUES(?,?,?,?,?)', (x['id'], x['claim'], x['amount'], x['actor'], 'signed'))
        db.execute("UPDATE credit_claims SET status='settled' WHERE id=?", (x['claim'],))
        return {'decision': 'settled'}
    if op == 'reverse':
        receipt = one(db, 'SELECT * FROM settlement_receipts WHERE id=?', (x['receipt'],))
        if not receipt or receipt['status'] != 'signed' or not x.get('reason'):
            return {'decision': 'reversal_denied'}
        db.execute('INSERT INTO settlement_reversals VALUES(?,?,?,?)', (x['id'], x['receipt'], receipt['amount'], x['reason']))
        db.execute("UPDATE settlement_receipts SET status='reversed' WHERE id=?", (x['receipt'],))
        db.execute("UPDATE credit_claims SET status='reversed' WHERE id=?", (receipt['claim_id'],))
        return {'decision': 'reversed'}
    if op == 'split_period':
        if any(k in x for k in ('total', 'total_amount', 'allocations')) or x.get('approve') or x.get('action') == 'approve':
            result = approve_apportionment(db, x)
            if result.get('decision') == 'apportionment_approved':
                detail = result.pop('periods')
                result['decision'] = 'split_confirmed'
                result['periods'] = sorted(result['ledgers'])
                result['allocation'] = detail
            return result
        case = one(db, 'SELECT * FROM cause_cases WHERE id=?', (x['case'],))
        claims = many(db, 'SELECT * FROM credit_claims WHERE case_id=?', (x['case'],))
        if not case or case['status'] != 'decided' or len({c['period'] for c in claims}) < 2:
            return {'decision': 'not_cross_period'}
        return {'decision': 'split_confirmed', 'periods': sorted({c['period'] for c in claims})}

    # --- total-constrained cross-period apportionment ---
    if op in ('apportion', 'apportionment', 'approve_split', 'approve_apportionment',
              'allocate', 'allocation', 'create_apportionment', 'split_allocation', 'split'):
        return approve_apportionment(db, x)
    if op in ('reapportion', 'reallocate', 'reweight', 're_split', 'resplit'):
        return reapply_apportionment(db, x)
    if op in ('confirm_attribution', 'attribute_uid', 'attribute', 'confirm_uid',
              'uid_proof', 'prove_attribution', 'proof', 'resolve_uid_period',
              'confirm_period', 'assign_uid_period'):
        return confirm_attribution(db, x)
    if op in ('close_period', 'sign_period', 'settle_period', 'sign', 'close'):
        return close_period(db, x)
    if op in ('balances', 'balance', 'reconcile', 'apportion_status',
              'split_balances', 'ledger', 'conservation'):
        pid = x.get('plan') or x.get('id') or x.get('apportionment')
        status = reconcile_plan(db, pid)
        if not status:
            return {'decision': 'apportionment_not_found'}
        status['decision'] = 'balances_reconciled'
        return status
    if op in ('downgrade', 'reassess', 'review_adjust', 'adjust', 'review',
              'reduce_apportionment', 'write_down'):
        return downgrade(db, x)
    return {'decision': 'unknown_operation'}


# ---------------------------------------------------------------------------
# Evidence and parameter helpers
# ---------------------------------------------------------------------------

def _liable_evidence(db, case_id):
    """Return (case, {uid: evidence}) using cause.py's verifiable UID detail.

    Repeated sightings (multiple partitions/sources) stay one dict entry.
    """
    detail = liable_uid_detail(db, case_id)
    if detail is None:
        return None, {}
    evidence = {
        item['uid']: {
            'bad': True,
            'sources': set(item['sources']),
            'kinds': set(item['kinds']),
        }
        for item in detail['detail']
    }
    return detail['case'], evidence


def _normalize_periods(x):
    raw = x.get('periods')
    if raw is None:
        raw = x.get('allocations', x.get('shares', x.get('plan_periods')))
    caps = x.get('caps') if isinstance(x.get('caps'), dict) else (
        x.get('period_caps') if isinstance(x.get('period_caps'), dict) else {})
    cases = x.get('cases') if isinstance(x.get('cases'), dict) else {}
    out = []
    if isinstance(raw, dict):
        for period, value in raw.items():
            if isinstance(value, dict):
                out.append({
                    'period': period,
                    'cap': value.get('cap', value.get('max', value.get('limit', caps.get(period)))),
                    'case': value.get('case', value.get('case_id', cases.get(period))),
                })
            else:
                out.append({'period': period, 'cap': value, 'case': cases.get(period)})
    elif isinstance(raw, list):
        for value in raw:
            if isinstance(value, str):
                out.append({'period': value, 'cap': caps.get(value), 'case': cases.get(value)})
            elif isinstance(value, dict):
                period = value.get('period') or value.get('id') or value.get('name')
                out.append({
                    'period': period,
                    'cap': value.get('cap', value.get('max', value.get('limit', caps.get(period)))),
                    'case': value.get('case') or value.get('case_id') or cases.get(period),
                })
    return out


def _match_plan_period(db, case_id, period):
    """Find the approved schedule row constraining a raw claim, if any."""
    if not case_id or period is None:
        return None
    return one(
        db,
        'SELECT * FROM apportion_periods WHERE case_id=? AND period=? ORDER BY rowid DESC LIMIT 1',
        (case_id, period),
    )


def _plan_claim_ids(ap):
    ids = []
    if ap.get('claim_id'):
        ids.append(ap['claim_id'])
    try:
        ids.extend(json.loads(ap.get('extra_claims') or '[]'))
    except (ValueError, TypeError):
        pass
    # de-duplicate while preserving order
    seen = set()
    return [cid for cid in ids if not (cid in seen or seen.add(cid))]


def _claims_by_id(db, claim_ids):
    if not claim_ids:
        return []
    marks = ','.join('?' * len(claim_ids))
    return many(db, f'SELECT * FROM credit_claims WHERE id IN ({marks})', tuple(claim_ids))


def _signed_receipt_sum(db, claim_ids):
    if not claim_ids:
        return 0
    marks = ','.join('?' * len(claim_ids))
    return one(db, f"SELECT COALESCE(SUM(amount),0) n FROM settlement_receipts WHERE status='signed' AND claim_id IN ({marks})",
               tuple(claim_ids))['n']


def _claim_has_signed_receipt(db, claim_id):
    return one(db, "SELECT 1 FROM settlement_receipts WHERE claim_id=? AND status='signed' LIMIT 1", (claim_id,)) is not None


def _hamilton_lots(total_lots, weights, uids):
    """Largest-remainder allocation of indivisible lots by integer weights."""
    weight_sum = sum(weights[u] for u in uids)
    if weight_sum <= 0:
        return {u: 0 for u in uids}
    quotas = {u: total_lots * weights[u] // weight_sum for u in uids}
    remainder = total_lots - sum(quotas.values())
    order = sorted(uids, key=lambda u: (-(total_lots * weights[u] % weight_sum), u))
    for uid in order[:max(0, remainder)]:
        quotas[uid] += 1
    return quotas


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------

def _build_schedule(db, x):
    """Validate and compute the allocation schedule. Returns (error_dict, schedule)."""
    pid = x.get('id') or x.get('plan') or x.get('event_id')
    if not pid:
        return _err('apportionment_denied', reason='missing_plan_id'), None
    total = x.get('total')
    if total is None:
        total = x.get('total_amount', x.get('amount'))
    if not _is_int(total) or total <= 0:
        return _err('apportionment_denied', reason='total_must_be_positive_integer'), None
    unit = x.get('unit', x.get('min_unit', 1))
    if not _is_int(unit) or unit < 1:
        return _err('apportionment_denied', reason='unit_must_be_positive_integer'), None
    contract = CONTRACT_ALIASES.get(x.get('contract', x.get('close', 'next_bill')))
    if not contract:
        return _err('apportionment_denied', reason='unknown_contract'), None

    specs = _normalize_periods(x)
    if len(specs) < 2:
        return _err('apportionment_denied', reason='at_least_two_periods_required'), None
    names = [s['period'] for s in specs]
    if any(n is None for n in names) or len(set(names)) != len(names):
        return _err('apportionment_denied', reason='periods_must_be_named_and_distinct'), None
    for s in specs:
        if not _is_int(s['cap']) or s['cap'] < 0:
            return _err('apportionment_denied', reason='cap_must_be_nonnegative_integer', period=s['period']), None

    uid_periods = {}
    uid_weights = {}
    uid_order = []

    def touch(uid):
        if uid not in uid_periods:
            uid_periods[uid] = set()
            uid_order.append(uid)

    # Evidence derived from decided cause cases (verifiable UID detail).
    for s in specs:
        if not s['case']:
            continue
        case, evidence = _liable_evidence(db, s['case'])
        if not case:
            return _err('apportionment_denied', reason='case_not_found', case=s['case']), None
        if case['status'] != 'decided':
            return _err('apportionment_denied', reason='case_not_decided', case=s['case'], status=case['status']), None
        if case['liable'] == 'waived':
            return _err('apportionment_denied', reason='case_waived', case=s['case']), None
        for uid in evidence:
            touch(uid)
            uid_periods[uid].add(s['period'])

    # Optional explicit UID evidence / weights (standalone fixtures and proofs).
    explicit = x.get('uids') or x.get('uid_evidence')
    assignments = x.get('assignments') or x.get('uid_periods') or {}
    forced_conflict = set(x.get('conflict_uids') or [])
    if isinstance(assignments, dict):
        for uid, target in assignments.items():
            touch(uid)
            if isinstance(target, (list, tuple)):
                for p in target:
                    uid_periods[uid].add(p)
            elif target in (None, 'conflict', 'pending', 'conflicted'):
                if target != 'pending':
                    forced_conflict.add(uid)
            else:
                uid_periods[uid].add(target)
    if isinstance(explicit, list):
        for item in explicit:
            if isinstance(item, str):
                touch(item)
            elif isinstance(item, dict) and item.get('uid'):
                uid = item['uid']
                touch(uid)
                if _is_int(item.get('weight')) and item['weight'] > 0:
                    uid_weights[uid] = item['weight']
                target = item.get('period', item.get('home'))
                periods = item.get('periods')
                if periods:
                    for p in periods:
                        uid_periods[uid].add(p)
                elif target:
                    uid_periods[uid].add(target)
                if item.get('state') in ('conflict', 'conflicted') or item.get('conflict'):
                    forced_conflict.add(uid)
    weights_arg = x.get('weights') if isinstance(x.get('weights'), dict) else {}
    for uid, w in weights_arg.items():
        if _is_int(w) and w > 0:
            uid_weights[uid] = w

    uids = sorted(uid_periods)
    if not uids:
        return _err('apportionment_denied', reason='no_liable_uids'), None
    for uid in uids:
        uid_weights.setdefault(uid, 1)

    lots_total = total // unit
    dust = total - lots_total * unit
    lots = _hamilton_lots(lots_total, uid_weights, uids)

    uid_rows = []
    unattributed = 0
    for uid in uids:
        amount = lots[uid] * unit
        periods_seen = uid_periods[uid]
        if uid in forced_conflict or len(periods_seen) >= 2:
            state, home = 'conflicted', None
            unattributed += amount
        elif len(periods_seen) == 1:
            state, home = 'supported', next(iter(periods_seen))
        else:
            state, home = 'pending', None
            unattributed += amount
        uid_rows.append({
            'uid': uid, 'weight': uid_weights[uid], 'lots': lots[uid],
            'amount': amount, 'home': home, 'state': state,
        })

    period_rows = []
    cap_blocked = 0
    for s in specs:
        p = s['period']
        gross = sum(r['amount'] for r in uid_rows if r['home'] == p)
        cap_bookable = min(gross, s['cap'] - (s['cap'] % unit))
        blocked = gross - cap_bookable
        cap_blocked += blocked
        puids = sorted(r['uid'] for r in uid_rows if r['home'] == p)
        period_rows.append({
            'period': p, 'case': s['case'], 'cap': s['cap'], 'uids': puids,
            'supported': gross, 'authority': cap_bookable, 'blocked': blocked,
        })

    balances = {'dust': dust, 'unattributed': unattributed, 'cap_blocked': cap_blocked}
    conservation = sum(p['authority'] for p in period_rows) + sum(balances.values())
    if conservation != total:  # defensive invariant; arithmetic should always hold
        return _err('apportionment_denied', reason='allocation_not_conserved',
                    computed=conservation, total=total), None

    schedule = {
        'pid': pid, 'total': total, 'unit': unit, 'contract': contract,
        'tenant': x.get('tenant'), 'case_id': x.get('case', x.get('incident')),
        'created_by': x.get('actor'), 'periods': period_rows,
        'uids': uid_rows, 'balances': balances,
        'issue_claims': bool(x.get('issue_claims', x.get('create_claims', False))),
    }
    return None, schedule


def approve_apportionment(db, x):
    pid = x.get('id') or x.get('plan') or x.get('event_id')
    if pid and one(db, 'SELECT 1 FROM apportion_plans WHERE id=?', (pid,)):
        return _err('apportionment_exists', plan=pid)
    error, schedule = _build_schedule(db, x)
    if error:
        return error
    if schedule['issue_claims'] and not schedule['tenant']:
        return _err('apportionment_denied', reason='tenant_required_to_issue_claims')
    pid = schedule['pid']

    db.execute('INSERT INTO apportion_plans VALUES(?,?,?,?,?,?,?,?)', (
        pid, schedule['case_id'], schedule['tenant'], schedule['total'], schedule['unit'],
        schedule['contract'], 'approved', schedule['created_by']))
    for p in schedule['periods']:
        claim_id = None
        extra = []
        if schedule['issue_claims'] and p['authority'] > 0:
            claim_id = f'{pid}:{p["period"]}'
            db.execute('INSERT INTO credit_claims VALUES(?,?,?,?,?,?)', (
                claim_id, p['case'] or schedule['case_id'], schedule['tenant'],
                p['period'], p['authority'], 'draft'))
        db.execute('INSERT INTO apportion_periods VALUES(?,?,?,?,?,?,?,?,?,?)', (
            pid, p['period'], p['case'], p['cap'], json.dumps(p['uids'], ensure_ascii=False),
            p['supported'], p['authority'], claim_id, json.dumps(extra), 0))
    for u in schedule['uids']:
        db.execute('INSERT INTO apportion_uids VALUES(?,?,?,?,?,?,?)', (
            pid, u['uid'], u['weight'], u['lots'], u['amount'], u['home'], u['state']))
    for name in BALANCE_NAMES:
        db.execute('INSERT INTO apportion_balances VALUES(?,?,?)', (pid, name, schedule['balances'][name]))

    status = reconcile_plan(db, pid)
    status['decision'] = 'apportionment_approved'
    status['contract'] = schedule['contract']
    return status


# ---------------------------------------------------------------------------
# Re-weighting (locked once a period is signed)
# ---------------------------------------------------------------------------

def reapply_apportionment(db, x):
    pid = x.get('plan') or x.get('id')
    plan = one(db, 'SELECT * FROM apportion_plans WHERE id=?', (pid,)) if pid else None
    if not plan:
        return {'decision': 'apportionment_not_found'}
    periods = many(db, 'SELECT * FROM apportion_periods WHERE plan_id=? ORDER BY rowid', (pid,))
    for ap in periods:
        ids = _plan_claim_ids(ap)
        if _signed_receipt_sum(db, ids) > 0 or ap['closed']:
            return _err('reapportion_denied', reason='period_already_signed', period=ap['period'])

    # Rebuild from the stored request plus any overrides supplied here.
    rebuilt_input = dict(x)
    rebuilt_input['id'] = pid
    rebuilt_input.setdefault('total', plan['total'])
    rebuilt_input.setdefault('unit', plan['unit'])
    rebuilt_input.setdefault('contract', plan['contract'])
    rebuilt_input['periods'] = [
        {'period': ap['period'], 'cap': ap['cap'], 'case': ap['case_id']} for ap in periods
    ]
    existing_uids = many(db, 'SELECT * FROM apportion_uids WHERE plan_id=?', (pid,))
    rebuilt_input['uids'] = [
        {'uid': u['uid'], 'weight': u['weight'],
         **({'period': u['home_period']} if u['home_period'] else {'state': u['state']})}
        for u in existing_uids
    ]
    error, schedule = _build_schedule(db, rebuilt_input)
    if error:
        error['decision'] = 'reapportion_denied'
        return error

    db.execute('UPDATE apportion_plans SET total=?,unit=?,contract=? WHERE id=?',
               (schedule['total'], schedule['unit'], schedule['contract'], pid))
    for p in schedule['periods']:
        db.execute('UPDATE apportion_periods SET cap=?,uids=?,supported=?,authority=? WHERE plan_id=? AND period=?',
                   (p['cap'], json.dumps(p['uids'], ensure_ascii=False), p['supported'], p['authority'], pid, p['period']))
        ap = one(db, 'SELECT * FROM apportion_periods WHERE plan_id=? AND period=?', (pid, p['period']))
        if ap and ap['claim_id']:
            claim = one(db, 'SELECT * FROM credit_claims WHERE id=?', (ap['claim_id'],))
            if claim and claim['status'] == 'draft':
                db.execute('UPDATE credit_claims SET amount=? WHERE id=?', (p['authority'], claim['id']))
    for u in schedule['uids']:
        db.execute('UPDATE apportion_uids SET weight=?,lots=?,amount=?,home_period=?,state=? WHERE plan_id=? AND uid=?',
                   (u['weight'], u['lots'], u['amount'], u['home'], u['state'], pid, u['uid']))
    for name in BALANCE_NAMES:
        db.execute('UPDATE apportion_balances SET amount=? WHERE plan_id=? AND name=?',
                   (schedule['balances'][name], pid, name))
    status = reconcile_plan(db, pid)
    status['decision'] = 'reapportioned'
    return status


# ---------------------------------------------------------------------------
# Period close / signing
# ---------------------------------------------------------------------------

def close_period(db, x):
    pid = x.get('plan')
    period = x.get('period')
    ap = one(db, 'SELECT * FROM apportion_periods WHERE plan_id=? AND period=?', (pid, period)) if pid else None
    if not ap:
        return {'decision': 'apportionment_not_found'}
    if ap['closed']:
        return _err('period_already_closed', plan=pid, period=period)
    claim_id = ap['claim_id']
    claim = one(db, 'SELECT * FROM credit_claims WHERE id=?', (claim_id,)) if claim_id else None
    if claim and claim['status'] == 'draft':
        if not x.get('actor') or not x.get('id'):
            return _err('period_close_denied', reason='actor_and_receipt_id_required')
        amount = x.get('amount', claim['amount'])
        if amount != claim['amount']:
            return {'decision': 'settlement_denied'}
        case = one(db, 'SELECT * FROM cause_cases WHERE id=?', (claim['case_id'],))
        topo = one(db, 'SELECT * FROM topology WHERE id=?', (case['topology_id'],)) if case else None
        if not case or case['status'] != 'decided' or not topo or topo['generation'] != case['topology_generation']:
            return {'decision': 'source_changed'}
        db.execute('INSERT INTO settlement_receipts VALUES(?,?,?,?,?)',
                   (x['id'], claim['id'], amount, x['actor'], 'signed'))
        db.execute("UPDATE credit_claims SET status='settled' WHERE id=?", (claim['id'],))
    elif claim and claim['status'] == 'settled':
        pass
    elif claim:
        return _err('period_close_denied', reason=f'claim_{claim["status"]}')
    elif ap['authority'] > 0:
        return _err('period_close_denied', reason='claim_required_before_close')
    db.execute('UPDATE apportion_periods SET closed=1 WHERE plan_id=? AND period=?', (pid, period))
    status = reconcile_plan(db, pid)
    status['decision'] = 'period_closed'
    status['closed_period'] = period
    return status


# ---------------------------------------------------------------------------
# Late period-attribution proof
# ---------------------------------------------------------------------------

def confirm_attribution(db, x):
    pid = x.get('plan')
    uid = x.get('uid')
    period = x.get('period')
    proof_id = x.get('id') or x.get('event_id')
    actor = x.get('actor') or x.get('reviewer')
    plan = one(db, 'SELECT * FROM apportion_plans WHERE id=?', (pid,)) if pid else None
    au = one(db, 'SELECT * FROM apportion_uids WHERE plan_id=? AND uid=?', (pid, uid)) if pid and uid else None
    ap = one(db, 'SELECT * FROM apportion_periods WHERE plan_id=? AND period=?', (pid, period)) if pid and period else None
    if not plan or not au or not ap or not proof_id:
        return {'decision': 'attribution_denied', 'reason': 'unknown_plan_uid_or_period'}
    if not actor:
        return {'decision': 'attribution_denied', 'reason': 'actor_required'}
    if one(db, 'SELECT 1 FROM uid_attributions WHERE id=?', (proof_id,)):
        return _err('attribution_exists', id=proof_id)
    if au['state'] not in ('conflicted', 'pending'):
        return _err('uid_already_attributed', uid=uid, state=au['state'])

    unit = plan['unit']
    amount = au['amount']
    cap_max = ap['cap'] - (ap['cap'] % unit)
    claim_ids = _plan_claim_ids(ap)
    active_booked = sum(c['amount'] for c in _claims_by_id(db, claim_ids) if c['status'] != 'reversed')
    room = max(0, cap_max - active_booked)
    booked = min(amount, room)
    blocked = amount - booked

    signed = _signed_receipt_sum(db, claim_ids) > 0 or bool(ap['closed'])
    if booked == 0:
        routing = 'cap_blocked'
    elif not signed:
        routing = 'open_share'
    else:
        routing = plan['contract']  # next_bill | return_period
    new_claim_id = None
    if booked > 0:
        if not signed and ap['claim_id']:
            main_claim = one(db, 'SELECT * FROM credit_claims WHERE id=?', (ap['claim_id'],))
            if main_claim and main_claim['status'] == 'draft':
                db.execute('UPDATE credit_claims SET amount=? WHERE id=?', (main_claim['amount'] + booked, main_claim['id']))
                db.execute('UPDATE apportion_periods SET authority=authority+? WHERE plan_id=? AND period=?',
                           (booked, pid, period))
                new_claim_id = main_claim['id']
                routing = 'open_share'
        if new_claim_id is None:
            suffix = 'forward' if (signed and plan['contract'] == 'next_bill') else (
                'catchup' if signed else 'share')
            if signed:
                routing = plan['contract']  # next_bill | return_period
            new_claim_id = f'{pid}:{period}:{suffix}:{proof_id}'
            db.execute('INSERT INTO credit_claims VALUES(?,?,?,?,?,?)', (
                new_claim_id, ap['case_id'] or plan['case_id'], plan['tenant'], period, booked, 'draft'))
            current_extra = json.loads(ap.get('extra_claims') or '[]')
            db.execute('UPDATE apportion_periods SET authority=authority+?, extra_claims=? WHERE plan_id=? AND period=?',
                       (booked, json.dumps(current_extra + [new_claim_id]), pid, period))

    row = one(db, 'SELECT * FROM apportion_balances WHERE plan_id=? AND name=?', (pid, 'unattributed'))
    db.execute('UPDATE apportion_balances SET amount=? WHERE plan_id=? AND name=?',
               (max(0, row['amount'] - amount), pid, 'unattributed'))
    if blocked:
        db.execute('UPDATE apportion_balances SET amount=amount+? WHERE plan_id=? AND name=?',
                   (blocked, pid, 'cap_blocked'))
    db.execute("UPDATE apportion_uids SET state='confirmed', home_period=? WHERE plan_id=? AND uid=?",
               (period, pid, uid))
    db.execute('INSERT INTO uid_attributions VALUES(?,?,?,?,?,?,?,?,?)', (
        proof_id, pid, uid, period, actor, new_claim_id, amount, routing, 'applied'))

    status = reconcile_plan(db, pid)
    status.update({
        'decision': 'attribution_confirmed',
        'uid': uid, 'period': period, 'uid_amount': amount,
        'booked': booked, 'cap_blocked': blocked,
        'routing': routing, 'claim_id': new_claim_id,
    })
    return status


# ---------------------------------------------------------------------------
# Retrospective downgrade
# ---------------------------------------------------------------------------

def downgrade(db, x):
    pid = x.get('plan') or x.get('id')
    event_id = x.get('event_id') or x.get('id')
    plan = one(db, 'SELECT * FROM apportion_plans WHERE id=?', (pid,)) if pid else None
    if not plan:
        return {'decision': 'apportionment_not_found'}

    removed_uids = x.get('remove_uids') or []
    reduction = None
    if _is_int(x.get('new_total')) or _is_int(x.get('total')):
        new_total = x.get('new_total', x.get('total'))
        if not _is_int(new_total) or new_total < 0 or new_total >= plan['total']:
            return _err('downgrade_denied', reason='new_total_must_be_lower_nonnegative_integer')
        reduction = plan['total'] - new_total
    elif removed_uids:
        rows = [one(db, 'SELECT * FROM apportion_uids WHERE plan_id=? AND uid=?', (pid, u)) for u in removed_uids]
        if any(r is None for r in rows):
            return _err('downgrade_denied', reason='unknown_uid_in_remove_uids')
        if any(r['state'] == 'downgraded' for r in rows):
            return _err('downgrade_denied', reason='uid_already_downgraded')
        reduction = sum(r['amount'] for r in rows)
    elif _is_int(x.get('reduce', x.get('amount'))):
        reduction = x.get('reduce', x.get('amount'))
        if reduction <= 0:
            return _err('downgrade_denied', reason='reduce_must_be_positive')
    else:
        return _err('downgrade_denied', reason='new_total_remove_uids_or_reduce_required')
    if reduction <= 0:
        return _err('downgrade_denied', reason='reduction_must_be_positive')

    bal_rows = many(db, 'SELECT * FROM apportion_balances WHERE plan_id=?', (pid,))
    named_available = sum(r['amount'] for r in bal_rows)

    periods = many(db, 'SELECT * FROM apportion_periods WHERE plan_id=? ORDER BY rowid', (pid,))
    unsigned_claims = []
    reversed_claims = []
    prior_by_claim = {}
    for adj in many(db, "SELECT detail FROM apportion_adjustments WHERE plan_id=? AND kind='downgrade'", (pid,)):
        try:
            prior_detail = json.loads(adj['detail'] or '{}')
            for cid, amt in prior_detail.get('writeoffs', []):
                prior_by_claim[cid] = prior_by_claim.get(cid, 0) + amt
        except (ValueError, TypeError):
            pass
    prior_writeoffs = sum(prior_by_claim.values())
    for ap in periods:
        for cid in _plan_claim_ids(ap):
            claim = one(db, 'SELECT * FROM credit_claims WHERE id=?', (cid,))
            if not claim:
                continue
            if claim['status'] == 'reversed':
                reversed_claims.append(claim)
            elif not _claim_has_signed_receipt(db, cid):
                unsigned_claims.append((ap, claim))
    reversed_available = sum(c['amount'] for c in reversed_claims) - prior_writeoffs
    available = named_available + sum(c['amount'] for _, c in unsigned_claims) + reversed_available
    if reduction > available:
        locked_signed = _signed_receipt_sum(db, [cid for ap in periods for cid in _plan_claim_ids(ap)])
        return _err('downgrade_locked_by_signed', reduction=reduction, adjustable=available,
                    signed_locked=locked_signed)

    remaining = reduction
    detail = {'balances': [], 'claim_reductions': [], 'writeoffs': []}

    for name in BALANCE_NAMES:
        if remaining <= 0:
            break
        row = one(db, 'SELECT * FROM apportion_balances WHERE plan_id=? AND name=?', (pid, name))
        if not row or row['amount'] <= 0:
            continue
        take = min(row['amount'], remaining)
        db.execute('UPDATE apportion_balances SET amount=? WHERE plan_id=? AND name=?',
                   (row['amount'] - take, pid, name))
        detail['balances'].append([name, take])
        remaining -= take

    for ap, claim in sorted(unsigned_claims, key=lambda item: (item[0]['period'], item[1]['id'])):
        if remaining <= 0:
            break
        take = min(claim['amount'], remaining)
        if take <= 0:
            continue
        db.execute('UPDATE credit_claims SET amount=? WHERE id=?', (claim['amount'] - take, claim['id']))
        db.execute('UPDATE apportion_periods SET authority=MAX(0,authority-?) WHERE plan_id=? AND period=?',
                   (take, pid, ap['period']))
        detail['claim_reductions'].append([claim['id'], take])
        remaining -= take

    for claim in sorted(reversed_claims, key=lambda c: c['id']):
        if remaining <= 0:
            break
        already = prior_by_claim.get(claim['id'], 0)
        take = min(claim['amount'] - already, remaining)
        if take <= 0:
            continue
        detail['writeoffs'].append([claim['id'], take])
        remaining -= take

    if remaining != 0:  # validation above makes this unreachable; keep transaction honest
        raise RuntimeError('downgrade allocation failed conservation')

    if removed_uids:
        for uid in removed_uids:
            db.execute("UPDATE apportion_uids SET state='downgraded' WHERE plan_id=? AND uid=?", (pid, uid))
    db.execute('UPDATE apportion_plans SET total=total-? WHERE id=?', (reduction, pid))
    db.execute('INSERT INTO apportion_adjustments VALUES(?,?,?,?,?,?)', (
        event_id, pid, 'downgrade', reduction,
        json.dumps(detail, ensure_ascii=False, sort_keys=True), x.get('reason')))

    status = reconcile_plan(db, pid)
    status['decision'] = 'downgrade_applied'
    status['reduced'] = reduction
    status['adjustment'] = detail
    return status


# ---------------------------------------------------------------------------
# Reconciliation: two period books plus named unallocated balances
# ---------------------------------------------------------------------------

def reconcile_plan(db, pid):
    plan = one(db, 'SELECT * FROM apportion_plans WHERE id=?', (pid,)) if pid else None
    if not plan:
        return None
    periods = many(db, 'SELECT * FROM apportion_periods WHERE plan_id=? ORDER BY rowid', (pid,))
    balance_rows = many(db, 'SELECT name, amount FROM apportion_balances WHERE plan_id=? ORDER BY name', (pid,))
    balances = {name: 0 for name in BALANCE_NAMES}
    for r in balance_rows:
        balances[r['name']] = r['amount']

    written_off = 0
    for adj in many(db, "SELECT detail FROM apportion_adjustments WHERE plan_id=? AND kind='downgrade'", (pid,)):
        try:
            written_off += sum(amt for _, amt in json.loads(adj['detail'] or '{}').get('writeoffs', []))
        except (ValueError, TypeError):
            pass

    period_view = []
    ledgers = {}
    signed = {}
    active_total = 0
    unfiled_total = 0
    reversed_total = 0
    for ap in periods:
        claim_ids = _plan_claim_ids(ap)
        claims = _claims_by_id(db, claim_ids)
        active_sum = sum(c['amount'] for c in claims if c['status'] != 'reversed')
        reversed_sum = sum(c['amount'] for c in claims if c['status'] == 'reversed')
        signed_sum = _signed_receipt_sum(db, claim_ids)
        # Authority is covered by either a live claim or a claim that was later
        # reversed (the reversed principal is reconciled separately below).
        unfiled = max(0, ap['authority'] - active_sum - reversed_sum)
        active_total += active_sum
        reversed_total += reversed_sum
        unfiled_total += unfiled
        ledgers[ap['period']] = active_sum
        signed[ap['period']] = signed_sum
        try:
            puids = json.loads(ap['uids'] or '[]')
        except (ValueError, TypeError):
            puids = []
        period_view.append({
            'period': ap['period'],
            'case': ap['case_id'],
            'cap': ap['cap'],
            'uids': puids,
            'supported': ap['supported'],
            'authority': ap['authority'],
            'closed': bool(ap['closed']),
            'claim_id': ap['claim_id'],
            'claims': [{'id': c['id'], 'amount': c['amount'], 'status': c['status']} for c in claims],
            'ledger': active_sum,
            'signed': signed_sum,
            'unfiled': unfiled,
        })

    named_total = sum(balances.values())
    reversed_net = max(0, reversed_total - written_off)
    unallocated = named_total + unfiled_total
    accounted = active_total + unallocated + reversed_net
    shares = {p['period']: p['authority'] for p in period_view}
    return {
        'plan': pid,
        'total': plan['total'],
        'unit': plan['unit'],
        'contract': plan['contract'],
        'status': plan['status'],
        'periods': period_view,
        'shares': shares,
        'ledgers': ledgers,
        'signed': signed,
        'balances': balances,
        'named_balance_total': named_total,
        'unfiled': unfiled_total,
        'unallocated': unallocated,
        'reversed': reversed_net,
        'written_off': written_off,
        'ledger_total': active_total,
        'accounted': accounted,
        'conserved': accounted == plan['total'],
    }

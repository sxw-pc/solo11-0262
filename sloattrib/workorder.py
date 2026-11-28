"""Reentrant switch-over work orders for moving pump P from rack A to rack B.

A work order never commands a valve. It is an authorised, persisted runbook
whose only job is to say whether the physical facts now support the next
stage. Advancing reads the real valve positions, pump protection state,
emergency-branch validation and rack blocks, so out-of-order or duplicated
completion receipts are harmless: advancing again from the same field state
reaches the same stage. Two authorisations exist:

* ``bypass`` keeps continuous cooling through the isolated emergency pump K,
  which must be authorised and validated on A before P is unloaded.
* ``downtime`` does not use K: A is drained first and its blocked requests are
  genuine outage evidence until cooling returns.

Both orders end with B connected to a runnable P.
"""
from .coolant import _both_valves, supplier
from .store import one

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_orders(
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, main_pump TEXT NOT NULL,
    emergency_pump TEXT, rack_a TEXT NOT NULL, rack_b TEXT NOT NULL,
    stage TEXT NOT NULL, actor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_order_progress(
    work_order TEXT NOT NULL, stage TEXT NOT NULL, reached_tick INTEGER NOT NULL,
    PRIMARY KEY (work_order, stage)
);
"""

BYPASS_STAGES = (
    'authorised',
    'emergency_validated',
    'a_on_emergency',
    'p_unloaded',
    'b_connected',
    'completed',
)
DOWNTIME_STAGES = (
    'authorised',
    'a_drained',
    'p_unloaded',
    'b_connected',
    'outage_recorded',
    'completed',
)


def _stage_ready(db, order, stage):
    p, k, a, b = order['main_pump'], order['emergency_pump'], order['rack_a'], order['rack_b']
    pump = one(db, 'SELECT * FROM cool_pumps WHERE id=?', (p,))
    if stage == 'emergency_validated':
        grant = one(db, 'SELECT status FROM cool_branch_grants WHERE pump=? AND rack=?', (k, a))
        return bool(grant and grant['status'] == 'validated')
    if stage == 'a_on_emergency':
        return supplier(db, a) == k
    if stage == 'a_drained':
        return one(db, "SELECT 1 FROM rack_blocks WHERE rack=? AND status='active'", (a,)) is not None
    if stage == 'p_unloaded':
        return _both_valves(db, p, a, False)
    if stage == 'b_connected':
        return pump is not None and pump['status'] == 'running' and supplier(db, b) == p
    if stage == 'outage_recorded':
        return one(db, "SELECT 1 FROM rack_outages WHERE rack=? AND status='accepted_offline'", (a,)) is not None
    return False


def handle(db, command):
    op = command['op']
    if op == 'open':
        kind = command['kind']
        if kind not in ('bypass', 'downtime'):
            return {'decision': 'invalid_work_order'}
        identifier = command['id']
        if one(db, 'SELECT 1 FROM work_orders WHERE id=?', (identifier,)):
            return {'decision': 'work_order_exists'}
        if kind == 'bypass' and not command.get('emergency_pump'):
            return {'decision': 'invalid_work_order'}
        db.execute('INSERT INTO work_orders VALUES(?,?,?,?,?,?,?,?)',
                   (identifier, kind, command['main_pump'], command.get('emergency_pump'),
                    command['rack_a'], command['rack_b'], 'authorised', command.get('actor', 'owner')))
        db.execute('INSERT INTO work_order_progress VALUES(?,?,?)',
                   (identifier, 'authorised', command['tick']))
        return {'decision': 'work_order_open', 'stage': 'authorised'}
    if op != 'advance':
        return {'decision': 'unknown_operation'}
    order = one(db, 'SELECT * FROM work_orders WHERE id=?', (command['id'],))
    if order is None:
        return {'decision': 'work_order_missing'}
    stages = BYPASS_STAGES if order['kind'] == 'bypass' else DOWNTIME_STAGES
    index = stages.index(order['stage'])
    if index == len(stages) - 1:
        return {'decision': 'work_order_completed', 'stage': order['stage']}
    next_stage = stages[index + 1]
    # Completion is the runbook's terminal gate: reaching it means every prior
    # physical stage was verified in order, so it needs no extra field check.
    if next_stage != 'completed' and not _stage_ready(db, order, next_stage):
        return {'decision': 'stage_not_ready', 'stage': order['stage'], 'next_stage': next_stage}
    db.execute('UPDATE work_orders SET stage=? WHERE id=?', (next_stage, order['id']))
    db.execute('INSERT OR IGNORE INTO work_order_progress VALUES(?,?,?)',
               (order['id'], next_stage, command['tick']))
    if next_stage == 'completed':
        return {'decision': 'work_order_completed', 'stage': next_stage}
    return {'decision': 'stage_advanced', 'stage': next_stage}

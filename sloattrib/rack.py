"""Record service on racks currently supplied by a complete cooling loop.

The rack scheduler is persisted separately from the valve actuators. A serve
decision is taken against the loops that are physically in place and still
eligible at that instant; the recorded pump is real attribution evidence. A
drain order blocks new requests on a rack, and every rejection is retained so
the true no-service interval can be reconstructed instead of inferred from
dispatch intent.
"""
from .coolant import supplier
from .store import one

SCHEMA = """
CREATE TABLE IF NOT EXISTS cooled_requests(
    uid TEXT PRIMARY KEY, rack TEXT NOT NULL, tick INTEGER NOT NULL,
    pump TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rack_blocks(
    id TEXT PRIMARY KEY, rack TEXT NOT NULL, reason TEXT NOT NULL,
    status TEXT NOT NULL, drain_tick INTEGER NOT NULL, restore_tick INTEGER
);
CREATE TABLE IF NOT EXISTS rack_outages(
    id TEXT PRIMARY KEY, rack TEXT NOT NULL, start_tick INTEGER NOT NULL,
    end_tick INTEGER, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rack_rejections(
    uid TEXT PRIMARY KEY, rack TEXT NOT NULL, tick INTEGER NOT NULL,
    reason TEXT NOT NULL
);
"""


def _open_block(db, rack):
    return one(db, "SELECT * FROM rack_blocks WHERE rack=? AND status='active'", (rack,))


def handle(db, command):
    op = command['op']
    if op == 'serve':
        rack, uid, tick = command['rack'], command['uid'], command['tick']
        # A recorded rejection is durable evidence: re-driving the same request
        # returns the original physical decision instead of a new verdict.
        prior = one(db, 'SELECT reason FROM rack_rejections WHERE uid=?', (uid,))
        if prior is not None:
            return {'decision': prior['reason']}
        if _open_block(db, rack):
            reason = 'request_blocked'
            db.execute('INSERT OR IGNORE INTO rack_rejections VALUES(?,?,?,?)',
                       (uid, rack, tick, reason))
            return {'decision': reason}
        pump = supplier(db, rack)
        if pump is None:
            reason = 'coolant_unavailable'
            db.execute('INSERT OR IGNORE INTO rack_rejections VALUES(?,?,?,?)',
                       (uid, rack, tick, reason))
            return {'decision': reason}
        if one(db, 'SELECT 1 FROM cooled_requests WHERE uid=?', (uid,)):
            return {'decision': 'already_served'}
        db.execute('INSERT INTO cooled_requests VALUES(?,?,?,?)', (uid, rack, tick, pump))
        return {'decision': 'served', 'pump': pump}
    if op == 'drain':
        rack, reason = command['rack'], command.get('reason', 'work_order')
        if _open_block(db, rack):
            return {'decision': 'block_exists'}
        db.execute('INSERT INTO rack_blocks VALUES(?,?,?,?,?,?)',
                   (command['id'], rack, reason, 'active', command['tick'], None))
        db.execute('INSERT INTO rack_outages VALUES(?,?,?,?,?)',
                   (command['id'] + ':outage', rack, command['tick'], None, 'open'))
        return {'decision': 'rack_blocked'}
    if op == 'restore':
        rack = command['rack']
        block = _open_block(db, rack)
        if block is None:
            return {'decision': 'no_open_block'}
        if supplier(db, rack) is None:
            return {'decision': 'coolant_unavailable'}
        db.execute("UPDATE rack_blocks SET status='lifted', restore_tick=? WHERE id=?",
                   (command['tick'], block['id']))
        db.execute("UPDATE rack_outages SET end_tick=?, status='restored' WHERE id=?",
                   (command['tick'], block['id'] + ':outage'))
        return {'decision': 'rack_restored'}
    if op == 'accept_outage':
        # The outage stays physically open (no cooling loop is back); the
        # equipment owner only closes the evidence trail with this decision.
        rack = command['rack']
        outage = one(db, "SELECT * FROM rack_outages WHERE rack=? AND status='open'", (rack,))
        if outage is None:
            return {'decision': 'no_open_outage'}
        db.execute("UPDATE rack_outages SET end_tick=?, status='accepted_offline' WHERE id=?",
                   (command['tick'], outage['id']))
        return {'decision': 'outage_accepted_offline'}
    return {'decision': 'unknown_operation'}

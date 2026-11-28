"""Synthetic pumps, independent branch valves, and a shared flow limit.

Valve motion is a three-phase evidence trail: a queued command, an actuator
start, and the arrival of the actual valve position. Only an arrived command
moves a valve, so dispatch order never determines physical order. Opening a
pressurised supply valve before the matching return valve is in place trips the
pump protection. A late return cannot clear that trip: the pump stays down
until a separately permitted, physically performed reset.
"""
from .store import many, one

SCHEMA = """
CREATE TABLE IF NOT EXISTS cool_pumps(
    id TEXT PRIMARY KEY, units INTEGER NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cool_valves(
    id TEXT PRIMARY KEY, pump TEXT NOT NULL REFERENCES cool_pumps(id),
    rack TEXT NOT NULL, direction TEXT NOT NULL, opened INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cool_valve_commands(
    id TEXT PRIMARY KEY, valve TEXT NOT NULL, target INTEGER NOT NULL,
    status TEXT NOT NULL, queued_tick INTEGER, started_tick INTEGER,
    arrived_tick INTEGER
);
CREATE TABLE IF NOT EXISTS cool_trips(
    id TEXT PRIMARY KEY, pump TEXT NOT NULL, tick INTEGER NOT NULL,
    supply_valve TEXT NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cool_permits(
    id TEXT PRIMARY KEY, pump TEXT NOT NULL, actor TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cool_resets(
    id TEXT PRIMARY KEY, pump TEXT NOT NULL, permit TEXT NOT NULL,
    tick INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cool_branch_grants(
    id TEXT PRIMARY KEY, pump TEXT NOT NULL, rack TEXT NOT NULL,
    actor TEXT, status TEXT NOT NULL
);
"""

RUNNING = 'running'
TRIPPED = 'tripped'


def _open_loops(db):
    """Pump/rack pairs whose supply and return valves are physically open."""
    return many(db, '''SELECT pump, rack FROM cool_valves WHERE opened=1
        GROUP BY pump, rack HAVING COUNT(DISTINCT direction)=2''')


def _candidate_pumps(db, rack):
    """Pumps that can physically serve the rack right now.

    A tripped pump or one carrying more complete loops than its capacity is
    never eligible. When several pumps share a rack, the extra pumps are only
    allowed through an authorised and validated physically independent
    emergency branch; an unvalidated emergency pump is dropped while sharing.
    """
    loops = _open_loops(db)
    load = {}
    for loop in loops:
        load[loop['pump']] = load.get(loop['pump'], 0) + 1
    pumps_on_rack = sorted({loop['pump'] for loop in loops if loop['rack'] == rack})
    eligible = []
    for pump in pumps_on_rack:
        row = one(db, 'SELECT * FROM cool_pumps WHERE id=?', (pump,))
        if row is None or row['status'] != RUNNING or load[pump] > row['units']:
            continue
        # A grant row means this is a pre-registered physically independent
        # emergency branch. While several pumps share the rack such a branch is
        # only allowed once authorised and validated; ordinary pumps without a
        # grant keep their natural eligibility regardless of naming.
        grant = one(db, "SELECT status FROM cool_branch_grants WHERE pump=? AND rack=?",
                    (pump, rack))
        if grant is not None and len(pumps_on_rack) > 1 and grant['status'] != 'validated':
            continue
        eligible.append(pump)
    return eligible


def supplier(db, rack):
    candidates = _candidate_pumps(db, rack)
    return candidates[0] if candidates else None


def available(db, rack):
    return supplier(db, rack) is not None


def _within_capacity(db, pump):
    row = one(db, 'SELECT * FROM cool_pumps WHERE id=?', (pump,))
    return row is not None and row['status'] == RUNNING and \
        sum(loop['pump'] == pump for loop in _open_loops(db)) <= row['units']


def _both_valves(db, pump, rack, opened):
    rows = many(db, 'SELECT direction, opened FROM cool_valves WHERE pump=? AND rack=?',
                (pump, rack))
    return len(rows) == 2 and all(row['opened'] == int(opened) for row in rows)


def _move_valve(db, command_id, valve_id, target, tick):
    """Apply an arrived valve position and return the pump tripped, if any."""
    valve = one(db, 'SELECT * FROM cool_valves WHERE id=?', (valve_id,))
    db.execute('UPDATE cool_valves SET opened=? WHERE id=?', (int(target), valve_id))
    if not target or valve['direction'] != 'supply':
        return None
    back = one(db, "SELECT * FROM cool_valves WHERE pump=? AND rack=? AND direction='return'",
               (valve['pump'], valve['rack']))
    if back is not None and back['opened'] == 1:
        return None
    pump = one(db, 'SELECT * FROM cool_pumps WHERE id=?', (valve['pump'],))
    if pump is None or pump['status'] != RUNNING:
        return None
    db.execute("UPDATE cool_pumps SET status=? WHERE id=?", (TRIPPED, valve['pump']))
    db.execute('INSERT INTO cool_trips VALUES(?,?,?,?,?)',
               (command_id, valve['pump'], tick, valve_id, 'open'))
    return valve['pump']


def _hazard_open(db, pump):
    """An open supply valve whose matching return valve is not in place."""
    return one(db, """SELECT v.id FROM cool_valves v
        WHERE v.pump=? AND v.direction='supply' AND v.opened=1
        AND NOT EXISTS (SELECT 1 FROM cool_valves r WHERE r.pump=v.pump
            AND r.rack=v.rack AND r.direction='return' AND r.opened=1)""",
               (pump,)) is not None


def handle(db, command):
    op, identifier = command['op'], command['id']
    if op == 'pump':
        units = command['units']
        if type(units) is not int or units < 0 or one(db, 'SELECT 1 FROM cool_pumps WHERE id=?', (identifier,)):
            return {'decision': 'invalid_pump'}
        db.execute('INSERT INTO cool_pumps VALUES(?,?,?)', (identifier, units, RUNNING))
        return {'decision': 'pump_registered'}
    if op == 'valve':
        pump, rack, direction = (command[k] for k in ('pump', 'rack', 'direction'))
        if direction not in ('supply', 'return') or not one(db, 'SELECT 1 FROM cool_pumps WHERE id=?', (pump,)):
            return {'decision': 'invalid_valve'}
        if one(db, 'SELECT 1 FROM cool_valves WHERE id=? OR (pump=? AND rack=? AND direction=?)',
               (identifier, pump, rack, direction)):
            return {'decision': 'valve_exists'}
        db.execute('INSERT INTO cool_valves VALUES (?,?,?,?,0)', (identifier, pump, rack, direction))
        return {'decision': 'valve_registered'}
    if op == 'actuate':
        # Synchronous field shorthand: queue, start and arrival happen in one
        # observed event. It goes through exactly the same physical rules.
        if type(command['open']) is not bool or not one(db, 'SELECT 1 FROM cool_valves WHERE id=?', (identifier,)):
            return {'decision': 'invalid_actuation'}
        cmd_id = command['event_id'] + ':direct'
        if one(db, 'SELECT 1 FROM cool_valve_commands WHERE id=?', (cmd_id,)):
            return {'decision': 'command_exists'}
        tick = command.get('tick', 0)
        db.execute('INSERT INTO cool_valve_commands VALUES(?,?,?,?,?,?,?)',
                   (cmd_id, identifier, int(command['open']), 'arrived', tick, tick, tick))
        tripped = _move_valve(db, cmd_id, identifier, command['open'], tick)
        result = {'decision': 'actuated'}
        if tripped:
            result['tripped'] = tripped
        return result
    if op == 'queue':
        if type(command['open']) is not bool or not one(db, 'SELECT 1 FROM cool_valves WHERE id=?', (command['valve'],)):
            return {'decision': 'invalid_command'}
        if one(db, 'SELECT 1 FROM cool_valve_commands WHERE id=?', (identifier,)):
            return {'decision': 'command_exists'}
        active = one(db, "SELECT id FROM cool_valve_commands WHERE valve=? AND status IN ('queued','started')",
                     (command['valve'],))
        if active:
            return {'decision': 'command_pending'}
        db.execute('INSERT INTO cool_valve_commands VALUES(?,?,?,?,?,?,?)',
                   (identifier, command['valve'], int(command['open']), 'queued',
                    command['tick'], None, None))
        return {'decision': 'queued'}
    if op == 'start':
        cmd = one(db, 'SELECT * FROM cool_valve_commands WHERE id=?', (identifier,))
        if cmd is None:
            return {'decision': 'invalid_command'}
        if cmd['status'] == 'arrived':
            return {'decision': 'phase_superseded'}
        if cmd['status'] == 'superseded':
            return {'decision': 'stale_command'}
        if cmd['status'] == 'started':
            return {'decision': 'already_started'}
        db.execute('UPDATE cool_valve_commands SET status=?, started_tick=? WHERE id=?',
                   ('started', command['tick'], identifier))
        return {'decision': 'started'}
    if op == 'arrive':
        cmd = one(db, 'SELECT * FROM cool_valve_commands WHERE id=?', (identifier,))
        if cmd is None:
            return {'decision': 'invalid_command'}
        if cmd['status'] == 'arrived':
            return {'decision': 'already_arrived'}
        if cmd['status'] == 'superseded':
            return {'decision': 'stale_command'}
        # An arrival receipt is authoritative physical evidence: a missing or
        # late start receipt cannot hold a confirmed valve position back.
        db.execute('UPDATE cool_valve_commands SET status=?, arrived_tick=? WHERE id=?',
                   ('arrived', command['tick'], identifier))
        tripped = _move_valve(db, identifier, cmd['valve'], bool(cmd['target']), command['tick'])
        if tripped:
            return {'decision': 'pump_tripped', 'pump': tripped}
        return {'decision': 'arrived'}
    if op == 'register_branch':
        pump, rack = command['pump'], command['rack']
        if not one(db, 'SELECT 1 FROM cool_pumps WHERE id=?', (pump,)):
            return {'decision': 'invalid_grant'}
        if one(db, 'SELECT 1 FROM cool_branch_grants WHERE id=?', (identifier,)):
            return {'decision': 'grant_exists'}
        valves = many(db, 'SELECT direction FROM cool_valves WHERE pump=? AND rack=?', (pump, rack))
        if len({row['direction'] for row in valves}) != 2:
            return {'decision': 'branch_not_registered'}
        if one(db, 'SELECT 1 FROM cool_branch_grants WHERE pump=? AND rack=?', (pump, rack)):
            return {'decision': 'grant_exists'}
        # The physical branch exists in the valve topology; it still carries no
        # authorisation and cannot be used while pumps share a rack.
        db.execute('INSERT INTO cool_branch_grants VALUES(?,?,?,?,?)',
                   (identifier, pump, rack, None, 'registered'))
        return {'decision': 'branch_registered'}
    if op == 'grant_branch':
        pump, rack, actor = command['pump'], command['rack'], command.get('actor')
        if not actor:
            return {'decision': 'invalid_grant'}
        grant = one(db, 'SELECT * FROM cool_branch_grants WHERE pump=? AND rack=?', (pump, rack))
        if grant is None:
            return {'decision': 'branch_not_registered'}
        if grant['status'] in ('active', 'validated'):
            return {'decision': 'grant_exists'}
        db.execute("UPDATE cool_branch_grants SET actor=?, status='active' WHERE id=?",
                   (actor, grant['id']))
        return {'decision': 'branch_granted'}
    if op == 'validate_branch':
        pump, rack = command['pump'], command['rack']
        grant = one(db, 'SELECT * FROM cool_branch_grants WHERE pump=? AND rack=?', (pump, rack))
        if grant is None:
            return {'decision': 'branch_not_granted'}
        if grant['status'] == 'validated':
            return {'decision': 'branch_validated'}
        if not _both_valves(db, pump, rack, True) or not _within_capacity(db, pump):
            return {'decision': 'branch_not_ready'}
        db.execute("UPDATE cool_branch_grants SET status='validated' WHERE id=?", (grant['id'],))
        return {'decision': 'branch_validated'}
    if op == 'permit':
        pump, actor = command['pump'], command.get('actor')
        if not actor or not one(db, 'SELECT 1 FROM cool_pumps WHERE id=?', (pump,)):
            return {'decision': 'invalid_permit'}
        if one(db, 'SELECT 1 FROM cool_permits WHERE id=?', (identifier,)):
            return {'decision': 'permit_exists'}
        db.execute('INSERT INTO cool_permits VALUES(?,?,?,?)', (identifier, pump, actor, 'active'))
        return {'decision': 'permit_issued'}
    if op == 'reset':
        pump, permit_id = command['pump'], command['permit']
        pump_row = one(db, 'SELECT * FROM cool_pumps WHERE id=?', (pump,))
        permit = one(db, 'SELECT * FROM cool_permits WHERE id=? AND pump=?', (permit_id, pump))
        if pump_row is None:
            return {'decision': 'invalid_reset'}
        if permit is None or permit['status'] != 'active':
            return {'decision': 'reset_permit_invalid'}
        if _hazard_open(db, pump):
            return {'decision': 'reset_unsafe'}
        if pump_row['status'] == RUNNING:
            return {'decision': 'pump_running'}
        db.execute("UPDATE cool_pumps SET status=? WHERE id=?", (RUNNING, pump))
        db.execute("UPDATE cool_permits SET status='used' WHERE id=?", (permit_id,))
        db.execute("UPDATE cool_trips SET status='cleared' WHERE pump=? AND status='open'", (pump,))
        db.execute('INSERT INTO cool_resets VALUES(?,?,?,?)',
                   (identifier, pump, permit_id, command['tick']))
        return {'decision': 'pump_reset'}
    return {'decision': 'unknown_operation'}

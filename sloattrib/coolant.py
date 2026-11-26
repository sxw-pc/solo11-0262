"""Synthetic pump, independent branch valves, and a shared flow limit."""
from .store import many, one

SCHEMA = """
CREATE TABLE IF NOT EXISTS cool_pumps(id TEXT PRIMARY KEY, units INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cool_valves(
    id TEXT PRIMARY KEY, pump TEXT NOT NULL REFERENCES cool_pumps(id),
    rack TEXT NOT NULL, direction TEXT NOT NULL, opened INTEGER NOT NULL
);
"""


def available(db, rack):
    pairs = many(db, '''SELECT pump,rack FROM cool_valves WHERE opened=1
        GROUP BY pump,rack HAVING COUNT(DISTINCT direction)=2 ORDER BY pump,rack''')
    for pair in pairs:
        if pair['rack'] != rack:
            continue
        pump = one(db,'SELECT units FROM cool_pumps WHERE id=?',(pair['pump'],))
        connected = sum(item['pump']==pair['pump'] for item in pairs)
        if connected <= pump['units']:
            return True
    return False


def handle(db, command):
    op, identifier = command['op'], command['id']
    if op == 'pump':
        units = command['units']
        if type(units) is not int or units < 0 or one(db,'SELECT 1 FROM cool_pumps WHERE id=?',(identifier,)):
            return {'decision':'invalid_pump'}
        db.execute('INSERT INTO cool_pumps VALUES (?,?)',(identifier,units))
        return {'decision':'pump_registered'}
    if op == 'valve':
        pump, rack, direction = (command[k] for k in ('pump','rack','direction'))
        if direction not in ('supply','return') or not one(db,'SELECT 1 FROM cool_pumps WHERE id=?',(pump,)):
            return {'decision':'invalid_valve'}
        if one(db,'SELECT 1 FROM cool_valves WHERE id=? OR (pump=? AND rack=? AND direction=?)',(identifier,pump,rack,direction)):
            return {'decision':'valve_exists'}
        db.execute('INSERT INTO cool_valves VALUES (?,?,?,?,0)',(identifier,pump,rack,direction))
        return {'decision':'valve_registered'}
    if op != 'actuate':
        return {'decision':'unknown_operation'}
    if type(command['open']) is not bool or not one(db,'SELECT 1 FROM cool_valves WHERE id=?',(identifier,)):
        return {'decision':'invalid_actuation'}
    db.execute('UPDATE cool_valves SET opened=? WHERE id=?',(int(command['open']),identifier))
    return {'decision':'actuated'}

"""Record service on racks currently supplied by a complete cooling loop."""
from .coolant import available
from .store import one

SCHEMA = """
CREATE TABLE IF NOT EXISTS cooled_requests(
    uid TEXT PRIMARY KEY, rack TEXT NOT NULL, tick INTEGER NOT NULL
);
"""


def handle(db, command):
    if command['op'] != 'serve':
        return {'decision':'unknown_operation'}
    rack = command['rack']
    if not available(db,rack):
        return {'decision':'coolant_unavailable'}
    if one(db,'SELECT 1 FROM cooled_requests WHERE uid=?',(command['uid'],)):
        return {'decision':'already_served'}
    db.execute('INSERT INTO cooled_requests VALUES (?,?,?)',(command['uid'],rack,command['tick']))
    return {'decision':'served'}

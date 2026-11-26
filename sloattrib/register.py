"""Record synthetic votes and apply majority-backed register writes."""
from .replicas import members
from .store import one

SCHEMA = """
CREATE TABLE IF NOT EXISTS register_votes(
    epoch TEXT NOT NULL, node TEXT NOT NULL, slot INTEGER NOT NULL,
    proposal TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(epoch,node,slot)
);
CREATE TABLE IF NOT EXISTS register_writes(
    epoch TEXT NOT NULL, slot INTEGER NOT NULL, proposal TEXT NOT NULL,
    value TEXT NOT NULL, PRIMARY KEY(epoch,slot)
);
"""


def handle(db, command):
    epoch, slot = command['epoch'], command['slot']
    current = {row['node']: row['online'] for row in members(db, epoch)}
    if not current:
        return {'decision': 'no_membership'}
    key = (epoch, slot, command['proposal'], command['value'])
    if command['op'] == 'vote':
        node = command['node']
        if not current.get(node):
            return {'decision': 'member_unavailable'}
        if one(db, 'SELECT 1 FROM register_votes WHERE epoch=? AND node=? AND slot=?', (epoch,node,slot)):
            return {'decision': 'slot_already_voted'}
        db.execute('INSERT INTO register_votes (epoch,slot,proposal,value,node) VALUES (?,?,?,?,?)', (*key,node))
        return {'decision': 'vote_recorded'}
    if command['op'] != 'apply':
        return {'decision': 'unknown_operation'}
    if one(db, 'SELECT 1 FROM register_writes WHERE epoch=? AND slot=?', (epoch,slot)):
        return {'decision': 'slot_already_applied'}
    count = one(db, 'SELECT COUNT(*) AS n FROM register_votes WHERE epoch=? AND slot=? AND proposal=? AND value=?', key)['n']
    if count <= len(current)//2:
        return {'decision': 'quorum_missing', 'votes': count}
    db.execute('INSERT INTO register_writes VALUES (?,?,?,?)', key)
    return {'decision': 'written'}

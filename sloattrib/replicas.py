"""Synthetic membership and service availability for a replicated register."""
from .store import many, one

SCHEMA = """
CREATE TABLE IF NOT EXISTS replica_members(
    epoch TEXT NOT NULL, node TEXT NOT NULL, online INTEGER NOT NULL,
    PRIMARY KEY(epoch,node)
);
"""


def members(db, epoch):
    return many(db, 'SELECT node,online FROM replica_members WHERE epoch=? ORDER BY node', (epoch,))


def handle(db, command):
    key = (command['epoch'], command['node'])
    row = one(db, 'SELECT 1 FROM replica_members WHERE epoch=? AND node=?', key)
    if command['op'] == 'add':
        if row:
            return {'decision': 'member_exists'}
        db.execute('INSERT INTO replica_members VALUES (?,?,1)', key)
        return {'decision': 'member_added'}
    if command['op'] == 'online':
        if not row or type(command['online']) is not bool:
            return {'decision': 'invalid_status'}
        db.execute('UPDATE replica_members SET online=? WHERE epoch=? AND node=?', (int(command['online']), *key))
        return {'decision': 'status_updated'}
    return {'decision': 'unknown_operation'}

"""Synthetic membership and service availability for a replicated register."""
from .store import many, one

SCHEMA = """
CREATE TABLE IF NOT EXISTS replica_members(
    epoch TEXT NOT NULL, node TEXT NOT NULL, online INTEGER NOT NULL,
    PRIMARY KEY(epoch,node)
);
CREATE TABLE IF NOT EXISTS epoch_handovers(
    old_epoch TEXT PRIMARY KEY, new_epoch TEXT NOT NULL,
    mode TEXT NOT NULL, status TEXT NOT NULL
);
"""

HANDOVER_MODES = ('fulfill', 'withdraw')


def members(db, epoch):
    return many(db, 'SELECT node,online FROM replica_members WHERE epoch=? ORDER BY node', (epoch,))


def handover(db, epoch):
    return one(db, 'SELECT * FROM epoch_handovers WHERE old_epoch=?', (epoch,))


def handle(db, command):
    key = (command['epoch'], command['node']) if 'node' in command else None
    row = one(db, 'SELECT 1 FROM replica_members WHERE epoch=? AND node=?', key) if key else None
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
    if command['op'] == 'remove':
        if not row:
            return {'decision': 'member_unknown'}
        db.execute('DELETE FROM replica_members WHERE epoch=? AND node=?', key)
        return {'decision': 'member_removed'}
    if command['op'] == 'handover':
        old_epoch, new_epoch = command['epoch'], command['new_epoch']
        mode = command['mode']
        if old_epoch == new_epoch or mode not in HANDOVER_MODES:
            return {'decision': 'invalid_handover'}
        if not members(db, old_epoch) or not members(db, new_epoch):
            return {'decision': 'no_membership'}
        existing = handover(db, old_epoch)
        if existing:
            if existing['new_epoch'] == new_epoch and existing['mode'] == mode:
                return {'decision': 'handover_held', 'status': existing['status']}
            return {'decision': 'handover_conflict'}
        db.execute('INSERT INTO epoch_handovers VALUES(?,?,?,?)',
                   (old_epoch, new_epoch, mode,
                    'old_sealed' if mode == 'withdraw' else 'opened'))
        return {'decision': 'handover_opened', 'mode': mode, 'new_epoch': new_epoch,
                'voting': 'closed' if mode == 'withdraw' else 'open'}
    if command['op'] == 'seal':
        existing = handover(db, command['epoch'])
        if not existing:
            return {'decision': 'handover_missing'}
        if existing['status'] in ('old_sealed', 'completed'):
            return {'decision': 'voting_already_closed'}
        db.execute("UPDATE epoch_handovers SET status='old_sealed' WHERE old_epoch=?",
                   (command['epoch'],))
        return {'decision': 'voting_closed'}
    if command['op'] == 'complete':
        existing = handover(db, command['epoch'])
        if not existing or existing['new_epoch'] != command['new_epoch']:
            return {'decision': 'handover_missing'}
        if existing['status'] == 'completed':
            return {'decision': 'handover_held', 'status': 'completed'}
        db.execute("UPDATE epoch_handovers SET status='completed' WHERE old_epoch=?",
                   (command['epoch'],))
        return {'decision': 'new_group_initialized', 'new_epoch': command['new_epoch']}
    if command['op'] == 'handover_status':
        existing = handover(db, command['epoch'])
        if not existing:
            return {'decision': 'handover_missing'}
        return {'decision': 'handover_state', **existing}
    return {'decision': 'unknown_operation'}

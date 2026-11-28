"""Record synthetic votes and apply majority-backed register writes.

A logical position keeps one externally visible result across membership
handovers.  The voter scope and quorum of a position are frozen when the
position is first opened in an epoch: later removals, reconnections or a node
reappearing under the same name in a new epoch never alter the voting power of
the old position.  Handovers are re-entrant, so every command can be retried
while the deployment sequence is interrupted.
"""
import json

from .replicas import members, handover
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
CREATE TABLE IF NOT EXISTS register_slot_scopes(
    epoch TEXT NOT NULL, slot INTEGER NOT NULL, voters TEXT NOT NULL,
    quorum INTEGER NOT NULL, PRIMARY KEY(epoch,slot)
);
CREATE TABLE IF NOT EXISTS register_positions(
    slot INTEGER PRIMARY KEY, epoch TEXT NOT NULL, proposal TEXT,
    value TEXT, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS register_proposals(
    slot INTEGER NOT NULL, proposal TEXT NOT NULL, value TEXT NOT NULL,
    status TEXT NOT NULL, receipt TEXT, PRIMARY KEY(slot,proposal)
);
CREATE TABLE IF NOT EXISTS register_no_vote_confirmations(
    epoch TEXT NOT NULL, node TEXT NOT NULL, slot INTEGER NOT NULL,
    PRIMARY KEY(epoch,node,slot)
);
"""

OPENED = 'opened'
OLD_SEALED = 'old_sealed'
COMPLETED = 'completed'


def _scope(db, epoch, slot):
    return one(db, 'SELECT * FROM register_slot_scopes WHERE epoch=? AND slot=?', (epoch, slot))


def _open_scope(db, epoch, slot):
    """Freeze the voter roster and majority threshold at first use."""
    scope = _scope(db, epoch, slot)
    if scope:
        return scope
    voters = [row['node'] for row in members(db, epoch)]
    if not voters:
        return None
    scope = {'epoch': epoch, 'slot': slot,
             'voters': json.dumps(voters, ensure_ascii=False), 'quorum': len(voters) // 2 + 1}
    db.execute('INSERT INTO register_slot_scopes VALUES(?,?,?,?)',
               (epoch, slot, scope['voters'], scope['quorum']))
    return scope


def _position(db, slot):
    return one(db, 'SELECT * FROM register_positions WHERE slot=?', (slot,))


def _open_position(db, slot, epoch):
    position = _position(db, slot)
    if position:
        return position
    db.execute('INSERT INTO register_positions VALUES(?,?,?,?,?)',
               (slot, epoch, None, None, 'open'))
    return _position(db, slot)


def _proposal(db, slot, proposal):
    return one(db, 'SELECT * FROM register_proposals WHERE slot=? AND proposal=?', (slot, proposal))


def _ensure_proposal(db, slot, proposal, value):
    row = _proposal(db, slot, proposal)
    if not row:
        db.execute('INSERT INTO register_proposals VALUES(?,?,?,?,?)',
                   (slot, proposal, value, 'pending', None))
    return _proposal(db, slot, proposal)


def _receipt_id(slot, proposal):
    return f'receipt:slot{slot}:{proposal}'


def _mark_proposal(db, slot, proposal, value, status):
    receipt = _receipt_id(slot, proposal) if status in ('settled', 'withdrawn') else None
    if _proposal(db, slot, proposal):
        db.execute('UPDATE register_proposals SET status=?, value=?, receipt=? WHERE slot=? AND proposal=?',
                   (status, value, receipt, slot, proposal))
    else:
        db.execute('INSERT INTO register_proposals VALUES(?,?,?,?,?)',
                   (slot, proposal, value, status, receipt))
    return receipt


def _vote_count(db, epoch, slot, proposal):
    return one(db, 'SELECT COUNT(*) AS n FROM register_votes WHERE epoch=? AND slot=? AND proposal=?',
               (epoch, slot, proposal))['n']


def _incoming_block(db, epoch, slot):
    """A handover target may not touch a slot the old group still claims."""
    incoming = one(db, 'SELECT * FROM epoch_handovers WHERE new_epoch=?', (epoch,))
    if not incoming or not _scope(db, incoming['old_epoch'], slot):
        return None
    position = _position(db, slot)
    if position and position['status'] == 'settled':
        return None
    if incoming['mode'] == 'withdraw' and position and position['status'] == 'vacant':
        return None
    return incoming


def _handle_vote(db, command, late):
    epoch, slot = command['epoch'], command['slot']
    node, proposal, value = command['node'], command['proposal'], command['value']
    if not members(db, epoch):
        return {'decision': 'no_membership'}
    order = handover(db, epoch)
    sealed = bool(order and order['status'] in (OLD_SEALED, COMPLETED))
    if sealed and not late:
        return {'decision': 'voting_closed'}
    if not late and not any(r['node'] == node and r['online'] for r in members(db, epoch)):
        return {'decision': 'member_unavailable'}
    blocked = _incoming_block(db, epoch, slot)
    if blocked:
        return {'decision': 'old_position_unresolved', 'old_epoch': blocked['old_epoch']}
    scope = _open_scope(db, epoch, slot)
    if node not in json.loads(scope['voters']):
        return {'decision': 'outside_frozen_voters'}
    if one(db, 'SELECT 1 FROM register_no_vote_confirmations WHERE epoch=? AND node=? AND slot=?',
           (epoch, node, slot)):
        return {'decision': 'node_confirmed_no_vote'}
    existing = one(db, 'SELECT * FROM register_votes WHERE epoch=? AND node=? AND slot=?',
                   (epoch, node, slot))
    if existing:
        if existing['proposal'] == proposal and existing['value'] == value:
            return {'decision': 'slot_already_voted'}
        return {'decision': 'slot_vote_conflict'}
    if late and not sealed:
        return {'decision': 'handover_not_sealed'}
    if late:
        proof = command.get('cast_before_seal')
        if type(proof) is not bool or not proof:
            return {'decision': 'late_vote_needs_sealed_proof'}
    position = _open_position(db, slot, epoch)
    if position['status'] == 'settled':
        return {'decision': 'position_already_settled'}
    _ensure_proposal(db, slot, proposal, value)
    db.execute('INSERT INTO register_votes (epoch,slot,proposal,value,node) VALUES (?,?,?,?,?)',
               (epoch, slot, proposal, value, node))
    return {'decision': 'late_vote_recorded' if late else 'vote_recorded'}


def _handle_confirm(db, command):
    epoch, slot, node = command['epoch'], command['slot'], command['node']
    order = handover(db, epoch)
    if not order or order['mode'] != 'withdraw' or order['status'] == OPENED:
        return {'decision': 'withdraw_handover_not_closed'}
    scope = _scope(db, epoch, slot)
    if not scope or node not in json.loads(scope['voters']):
        return {'decision': 'outside_frozen_voters'}
    if one(db, 'SELECT 1 FROM register_votes WHERE epoch=? AND node=? AND slot=?',
           (epoch, node, slot)):
        return {'decision': 'node_already_voted'}
    if one(db, 'SELECT 1 FROM register_no_vote_confirmations WHERE epoch=? AND node=? AND slot=?',
           (epoch, node, slot)):
        return {'decision': 'no_vote_already_confirmed'}
    db.execute('INSERT INTO register_no_vote_confirmations VALUES(?,?,?)', (epoch, node, slot))
    return {'decision': 'no_vote_confirmed'}


def _accounting(db, epoch, slot, proposal):
    """Per-node traceable last vote state for every frozen voter."""
    scope = _scope(db, epoch, slot)
    voters = json.loads(scope['voters']) if scope else []
    account = {}
    for voter in voters:
        vote = one(db, 'SELECT proposal,value FROM register_votes WHERE epoch=? AND node=? AND slot=?',
                   (epoch, voter, slot))
        confirmed = one(db, 'SELECT 1 FROM register_no_vote_confirmations WHERE epoch=? AND node=? AND slot=?',
                        (epoch, voter, slot))
        if vote:
            account[voter] = {'state': 'voted', 'proposal': vote['proposal'], 'value': vote['value']}
        elif confirmed:
            account[voter] = {'state': 'no_vote_confirmed'}
        else:
            account[voter] = {'state': 'unaccounted'}
    count = _vote_count(db, epoch, slot, proposal)
    unaccounted = sorted(node for node, item in account.items() if item['state'] == 'unaccounted')
    return voters, account, count, unaccounted


def _settle_old(db, slot, epoch, proposal, value, count, scope):
    """Publish the unique result of an old group onto the logical position."""
    receipt = _mark_proposal(db, slot, proposal, value, 'settled')
    db.execute('INSERT INTO register_writes VALUES (?,?,?,?)', (epoch, slot, proposal, value))
    db.execute('UPDATE register_positions SET epoch=?, proposal=?, value=?, status=? WHERE slot=?',
               (epoch, proposal, value, 'settled', slot))
    return {'decision': 'written', 'epoch': epoch, 'proposal': proposal, 'value': value,
            'votes': count, 'quorum': scope['quorum'], 'receipt': receipt,
            'position_status': 'settled'}


def _handle_apply(db, command):
    epoch, slot = command['epoch'], command['slot']
    proposal, value = command['proposal'], command['value']
    if not members(db, epoch):
        return {'decision': 'no_membership'}
    scope = _open_scope(db, epoch, slot)
    position = _open_position(db, slot, epoch)
    if position['status'] == 'settled':
        if position['epoch'] == epoch and position['proposal'] == proposal:
            existing = _proposal(db, slot, proposal)
            return {'decision': 'slot_already_applied', 'proposal': proposal,
                    'value': position['value'], 'receipt': existing['receipt']}
        return {'decision': 'position_already_settled', 'epoch': position['epoch'],
                'proposal': position['proposal'], 'value': position['value']}
    blocked = _incoming_block(db, epoch, slot)
    if blocked:
        return {'decision': 'old_position_unresolved', 'old_epoch': blocked['old_epoch'],
                'position_status': position['status']}
    count = _vote_count(db, epoch, slot, proposal)
    order = handover(db, epoch)
    if count < scope['quorum']:
        return {'decision': 'quorum_missing', 'votes': count, 'quorum': scope['quorum']}
    if order:
        # A proven old-group majority must be handed over regardless of how
        # many controller receipts were lost.  The new group can never write
        # another value for this position.  On the fulfill route an opened
        # handover keeps the in-flight proposal electable on the old group.
        if order['mode'] == 'withdraw' and order['status'] == OPENED:
            return {'decision': 'withdraw_handover_not_closed'}
    # No handover, fulfill route still open, or an already sealed old group.
    return _settle_old(db, slot, epoch, proposal, value, count, scope)


def _handle_withdraw_apply(db, command):
    """Withdraw-route settlement backed by per-node traceable state."""
    epoch, slot = command['epoch'], command['slot']
    proposal, value = command['proposal'], command['value']
    order = handover(db, epoch)
    if not order or order['mode'] != 'withdraw' or order['status'] == OPENED:
        return {'decision': 'withdraw_handover_not_closed'}
    scope = _scope(db, epoch, slot)
    if not scope:
        return {'decision': 'position_never_opened'}
    position = _open_position(db, slot, epoch)
    if position['status'] == 'settled':
        return {'decision': 'position_already_settled', 'epoch': position['epoch'],
                'proposal': position['proposal'], 'value': position['value']}
    voters, account, count, unaccounted = _accounting(db, epoch, slot, proposal)
    if count >= scope['quorum']:
        return _settle_old(db, slot, epoch, proposal, value, count, scope)
    if unaccounted:
        # A single visible vote is not proof the proposal failed: an unseen
        # node may already hold a ballot that completes the majority.
        return {'decision': 'outcome_unverified', 'proposal': proposal,
                'votes': count, 'quorum': scope['quorum'],
                'unaccounted': unaccounted, 'voter_state': account}
    receipt = _mark_proposal(db, slot, proposal, value, 'withdrawn')
    db.execute('UPDATE register_positions SET status=? WHERE slot=?', ('vacant', slot))
    return {'decision': 'proposal_withdrawn', 'proposal': proposal, 'receipt': receipt,
            'voters': voters, 'voter_state': account, 'position_status': 'vacant'}


def _handle_read(db, command):
    position = _position(db, command['slot'])
    if not position:
        return {'decision': 'position_unknown', 'slot': command['slot']}
    result = {'decision': 'position_state', **position}
    if position['status'] == 'settled':
        result['readable'] = True
    return result


def _handle_proposal_status(db, command):
    row = _proposal(db, command['slot'], command['proposal'])
    if not row:
        return {'decision': 'proposal_unknown', 'slot': command['slot'],
                'proposal': command['proposal']}
    return {'decision': 'proposal_state', **row}


def _handle_voter_state(db, command):
    scope = _scope(db, command['epoch'], command['slot'])
    if not scope:
        return {'decision': 'position_never_opened'}
    _, account, _, _ = _accounting(db, command['epoch'], command['slot'], None)
    return {'decision': 'voter_state', 'slot': command['slot'], 'epoch': command['epoch'],
            'quorum': scope['quorum'], 'voter_state': account}


def handle(db, command):
    op = command['op']
    if op == 'vote':
        return _handle_vote(db, command, late=False)
    if op == 'deliver_late_vote':
        return _handle_vote(db, command, late=True)
    if op == 'confirm_no_vote':
        return _handle_confirm(db, command)
    if op == 'apply':
        return _handle_apply(db, command)
    if op == 'withdraw_apply':
        return _handle_withdraw_apply(db, command)
    if op == 'read':
        return _handle_read(db, command)
    if op == 'proposal_status':
        return _handle_proposal_status(db, command)
    if op == 'voter_state':
        return _handle_voter_state(db, command)
    return {'decision': 'unknown_operation'}

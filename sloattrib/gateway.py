"""Route connections to simulated listeners, driven only by inbound evidence.

A listener decides destination from the connection it received and local
state that has already taken effect: the listener registration, allocation
generations minted locally and locally sealed reclaims.  Business names,
client intent, or background cache tables are never treated as inbound
identity.  Connections without a service marker retain the old checkout
owner while that allocation remains viable; only a marker matching the
current allocation generation can reach the new owner.
"""
from .dns import resolution
from .store import one

SCHEMA = """
CREATE TABLE IF NOT EXISTS dns_listeners(
    address TEXT PRIMARY KEY, backend TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'plain', entry_reclaim TEXT
);
CREATE TABLE IF NOT EXISTS gateway_deliveries(
    event_id TEXT PRIMARY KEY, client TEXT NOT NULL, address TEXT NOT NULL,
    backend TEXT NOT NULL, at INTEGER NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0, marker TEXT
);
CREATE TABLE IF NOT EXISTS gateway_denials(
    event_id TEXT PRIMARY KEY, client TEXT NOT NULL, address TEXT,
    reason TEXT NOT NULL, at INTEGER NOT NULL
);
"""


def _latest_allocation(db, address):
    return one(db, 'SELECT * FROM dns_allocations WHERE address=? '
                   'ORDER BY generation DESC LIMIT 1', (address,))


def _open_reclaim(db, address):
    return one(db, "SELECT * FROM dns_reclaims WHERE address=? AND status='open' "
                   'ORDER BY rowid LIMIT 1', (address,))


def _deny(db, command, address, reason):
    db.execute('INSERT INTO gateway_denials VALUES (?,?,?,?,?)',
               (command['event_id'], command['client'], address, reason, command['at']))
    return {'decision': reason, 'address': address}


def _old_owner_backend(db, reclaim):
    """Locate the old owner's own listener via its current published address."""
    record = one(db, 'SELECT address FROM dns_records WHERE identity=?',
                 (reclaim['old_owner'],))
    if not record:
        return None
    listener = one(db, 'SELECT backend FROM dns_listeners WHERE address=?',
                   (record['address'],))
    return listener['backend'] if listener else None


def handle(db, command):
    op = command['op']

    if op == 'listen':
        kind = command.get('kind', 'plain')
        if kind not in ('plain', 'compat'):
            return {'decision': 'invalid_listener_kind'}
        reclaim_id = command.get('entry_reclaim')
        if kind == 'compat':
            if not reclaim_id:
                return {'decision': 'entry_reclaim_required'}
            reclaim = one(db, 'SELECT * FROM dns_reclaims WHERE id=?', (reclaim_id,))
            if not reclaim or reclaim['address'] != command['address'] or \
                    reclaim['mode'] != 'compatible':
                return {'decision': 'entry_reclaim_invalid'}
        else:
            reclaim_id = None
        db.execute('INSERT INTO dns_listeners VALUES (?,?,?,?) '
                   'ON CONFLICT(address) DO UPDATE SET backend=excluded.backend, '
                   'kind=excluded.kind, entry_reclaim=excluded.entry_reclaim',
                   (command['address'], command['backend'], kind, reclaim_id))
        return {'decision': 'listening', 'address': command['address'],
                'backend': command['backend'], 'kind': kind}

    if op == 'promote':
        listener = one(db, 'SELECT * FROM dns_listeners WHERE address=?',
                       (command['address'],))
        if not listener:
            return {'decision': 'listener_missing'}
        if listener['kind'] != 'compat':
            return {'decision': 'not_compat_entry'}
        reclaim = one(db, 'SELECT * FROM dns_reclaims WHERE id=?',
                      (listener['entry_reclaim'],))
        if not reclaim or reclaim['status'] != 'sealed':
            return {'decision': 'rights_outstanding'}
        db.execute("UPDATE dns_listeners SET kind='plain', entry_reclaim=NULL WHERE address=?",
                   (command['address'],))
        return {'decision': 'exclusive_listening', 'address': command['address'],
                'backend': listener['backend']}

    if op != 'deliver':
        return {'decision': 'unknown_operation'}

    client, at = command['client'], command['at']
    marker = command.get('marker')

    # New-style delivery: the connection proves its own allocation. No name,
    # no client cache lookup — the listener sees only the inbound token.
    if marker is not None:
        allocation = one(db, 'SELECT * FROM dns_allocations WHERE marker=?', (marker,))
        if not allocation or allocation['address'] != command.get('address'):
            return _deny(db, command, command.get('address'), 'stale_lease')
        latest = _latest_allocation(db, allocation['address'])
        if not latest or latest['generation'] != allocation['generation']:
            return _deny(db, command, allocation['address'], 'stale_lease')
        address = allocation['address']
        listener = one(db, 'SELECT * FROM dns_listeners WHERE address=?', (address,))
        if not listener:
            return _deny(db, command, address, 'listener_missing')
        reclaim = _open_reclaim(db, address)
        # While the previous generation still holds rights, an exclusive
        # listener cannot accept the new owner; a compat entry can.
        if reclaim and listener['kind'] == 'plain':
            return _deny(db, command, address, 'reclaim_pending')
        db.execute('INSERT INTO gateway_deliveries VALUES (?,?,?,?,?,?,?)',
                   (command['event_id'], client, address, listener['backend'], at,
                    allocation['generation'], marker))
        return {'decision': 'delivered', 'address': address,
                'backend': listener['backend'], 'generation': allocation['generation']}

    # Legacy delivery: address-only dispatch, keyed by the client's still
    # valid cached answer. The cache yields an address; nothing about the
    # client's business intent enters the routing decision.
    cached = resolution(db, client, command['name'], at)
    if not cached:
        return _deny(db, command, None, 'lookup_required')
    address = cached['address']
    listener = one(db, 'SELECT * FROM dns_listeners WHERE address=?', (address,))
    if not listener:
        return _deny(db, command, address, 'listener_missing')
    cache_row = one(db, 'SELECT generation FROM dns_caches WHERE client=? AND name=?',
                    (client, command['name']))
    cache_generation = cache_row['generation']
    reclaim = _open_reclaim(db, address)
    if reclaim:
        if reclaim['old_generation'] != cache_generation:
            # This answer was never part of the rights the open reclaim is
            # waiting on; it cannot ride the old owner's continuing rights.
            return _deny(db, command, address, 'stale_lease')
        if listener['kind'] == 'plain':
            return _deny(db, command, address, 'reclaim_pending')
        backend = _old_owner_backend(db, reclaim)
        if backend is None:
            return _deny(db, command, address, 'listener_missing')
    else:
        backend = listener['backend']
    db.execute('INSERT INTO gateway_deliveries VALUES (?,?,?,?,?,?,?)',
               (command['event_id'], client, address, backend, at,
                cache_generation, None))
    return {'decision': 'delivered', 'address': address, 'backend': backend,
            'generation': cache_generation}

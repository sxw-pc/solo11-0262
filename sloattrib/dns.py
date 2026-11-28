"""Offline service-name records with lifetime-limited client caches.

Address recycling handoff is modelled with explicit allocation generations.
Every time an address changes owner, a new generation with an unguessable
lease marker is minted; a connection marker only proves identity for the
single allocation generation it was minted for.  Reclaims progress only
through itemized, re-entrant evidence commands: recursive cache forwards
(emit/accept), per-branch withdrawals that bind the generation they actually
shut down, and an examination that re-derives exhaustion from those records
rather than from a central "who still references this" table.
"""
import hashlib

from .store import one

SCHEMA = """
CREATE TABLE IF NOT EXISTS dns_records(
    name TEXT PRIMARY KEY, address TEXT NOT NULL, identity TEXT NOT NULL, ttl INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dns_allocations(
    address TEXT NOT NULL, generation INTEGER NOT NULL,
    owner TEXT NOT NULL, marker TEXT NOT NULL, opened_at INTEGER NOT NULL,
    PRIMARY KEY(address, generation), UNIQUE(marker)
);
CREATE TABLE IF NOT EXISTS dns_reclaims(
    id TEXT PRIMARY KEY, address TEXT NOT NULL, old_generation INTEGER NOT NULL,
    new_generation INTEGER NOT NULL, old_owner TEXT NOT NULL, new_owner TEXT NOT NULL,
    mode TEXT NOT NULL, opened_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', sealed_at INTEGER, basis TEXT
);
CREATE TABLE IF NOT EXISTS dns_caches(
    client TEXT NOT NULL, name TEXT NOT NULL, address TEXT NOT NULL,
    identity TEXT NOT NULL, expires INTEGER NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
    provenance TEXT NOT NULL DEFAULT 'lookup', surrendered INTEGER NOT NULL DEFAULT 0,
    surrendered_at INTEGER, PRIMARY KEY(client, name)
);
CREATE TABLE IF NOT EXISTS dns_forwards(
    id TEXT PRIMARY KEY, address TEXT NOT NULL, generation INTEGER NOT NULL,
    name TEXT NOT NULL, upstream_client TEXT NOT NULL, downstream_client TEXT NOT NULL,
    upstream_expires INTEGER NOT NULL, emitted_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'in_flight', accepted_at INTEGER, downstream_expires INTEGER
);
CREATE TABLE IF NOT EXISTS dns_withdrawals(
    id TEXT PRIMARY KEY, address TEXT NOT NULL, generation INTEGER NOT NULL,
    client TEXT NOT NULL, at INTEGER NOT NULL,
    partial INTEGER NOT NULL, covered_forwards TEXT NOT NULL
);
"""


def resolution(db, client, name, at):
    return one(db, 'SELECT address,identity,expires FROM dns_caches '
               'WHERE client=? AND name=? AND expires>?', (client, name, at))


def _latest(db, address):
    return one(db, 'SELECT * FROM dns_allocations WHERE address=? '
               'ORDER BY generation DESC LIMIT 1', (address,))


def _mint(db, address, owner, at):
    latest = _latest(db, address)
    generation = 1 if not latest else latest['generation'] + 1
    # Deterministic, unguessable-from-outside marker: validating a marker
    # still requires the locally minted allocation row, and a marker never
    # matches any other (address, generation) pair.  Determinism is required
    # for exact event replay of the business state.
    marker = hashlib.sha256(
        f'lease:{address}:{generation}:{owner}:{at}'.encode()).hexdigest()
    db.execute('INSERT INTO dns_allocations VALUES (?,?,?,?,?)',
               (address, generation, owner, marker, at))
    return generation, marker


def _open_reclaim(db, rid, address, at, mode, old_owner, new_owner):
    latest = _latest(db, address)
    if not latest or latest['owner'] != old_owner:
        return {'decision': 'reclaim_owner_mismatch'}
    if one(db, "SELECT id FROM dns_reclaims WHERE address=? AND status='open'", (address,)):
        return {'decision': 'reclaim_already_open'}
    # A new recycling round always mints a fresh generation for the new
    # owner. Reusing a past allocation would let a previous-round marker
    # authenticate the new owner, which must never happen.
    new_generation, marker = _mint(db, address, new_owner, at)
    db.execute('INSERT INTO dns_reclaims VALUES (?,?,?,?,?,?,?,?,?,?,?)',
               (rid, address, latest['generation'], new_generation, old_owner, new_owner,
                mode, at, 'open', None, None))
    return {'decision': 'reclaim_opened', 'reclaim': rid, 'address': address,
            'mode': mode, 'old_generation': latest['generation'],
            'new_generation': new_generation, 'marker': marker}


def _outstanding_branches(db, reclaim, at):
    address, generation = reclaim['address'], reclaim['old_generation']
    branches = []
    for cache in _cache_branches(db, address, generation, at):
        branches.append(('cache', f"cache:{cache['client']}:{cache['name']}"))
    for forward in _forward_branches(db, address, generation, at):
        branches.append(('forward', f"forward:{forward['id']}"))
    return branches


def _cache_branches(db, address, generation, at=None):
    if at is None:
        return [dict(r) for r in db.execute(
            'SELECT * FROM dns_caches WHERE address=? AND generation=? AND surrendered=0',
            (address, generation))]
    return [dict(r) for r in db.execute(
            'SELECT * FROM dns_caches WHERE address=? AND generation=? '
            'AND surrendered=0 AND expires>?', (address, generation, at))]


def _forward_branches(db, address, generation, at=None):
    # An in-flight answer past its upstream deadline can no longer be
    # accepted downstream, so it stops being an outstanding branch.
    if at is None:
        return [dict(r) for r in db.execute(
            "SELECT * FROM dns_forwards WHERE address=? AND generation=? "
            "AND status='in_flight'", (address, generation))]
    return [dict(r) for r in db.execute(
        "SELECT * FROM dns_forwards WHERE address=? AND generation=? "
        "AND status='in_flight' AND upstream_expires>?",
        (address, generation, at))]


def handle(db, command):
    op = command['op']

    if op == 'publish':
        name = command['name']
        ttl = command['ttl']
        if type(ttl) is not int or ttl < 1:
            return {'decision': 'invalid_lifetime'}
        address, identity, at = command['address'], command['identity'], command.get('at', 0)
        latest = _latest(db, address)
        if latest and latest['owner'] == identity:
            generation, marker = latest['generation'], latest['marker']
        else:
            generation, marker = _mint(db, address, identity, at)
        db.execute('INSERT INTO dns_records VALUES (?,?,?,?) '
                   'ON CONFLICT(name) DO UPDATE SET address=excluded.address, '
                   'identity=excluded.identity, ttl=excluded.ttl',
                   (name, address, identity, ttl))
        return {'decision': 'record_live', 'address': address,
                'generation': generation, 'marker': marker}

    if op == 'lookup':
        name = command['name']
        client, at = command['client'], command['at']
        cached = resolution(db, client, name, at)
        if cached:
            alloc = _latest(db, cached['address'])
            return {'decision': 'cached', **cached,
                    'generation': alloc['generation'] if alloc else 0,
                    'marker': alloc['marker'] if alloc else None}
        record = one(db, 'SELECT address,identity,ttl FROM dns_records WHERE name=?', (name,))
        if not record:
            return {'decision': 'name_missing'}
        alloc = _latest(db, record['address'])
        expires = at + record['ttl']
        db.execute('INSERT INTO dns_caches VALUES (?,?,?,?,?,?,?,?,?) '
                   'ON CONFLICT(client,name) DO UPDATE SET address=excluded.address, '
                   'identity=excluded.identity, expires=excluded.expires, '
                   'generation=excluded.generation, provenance=excluded.provenance, '
                   'surrendered=0, surrendered_at=NULL',
                   (client, name, record['address'], record['identity'], expires,
                    alloc['generation'] if alloc else 0, 'lookup', 0, None))
        return {'decision': 'resolved', 'address': record['address'],
                'identity': record['identity'], 'expires': expires,
                'generation': alloc['generation'] if alloc else 0,
                'marker': alloc['marker'] if alloc else None}

    if op == 'recycle':
        if command['mode'] not in ('exclusive', 'compatible'):
            return {'decision': 'invalid_mode'}
        return _open_reclaim(db, command['reclaim'], command['address'],
                             command.get('at', 0), command['mode'],
                             command['old_owner'], command['new_owner'])

    if op == 'emit_forward':
        name = command['name']
        upstream = one(db, 'SELECT * FROM dns_caches WHERE client=? AND name=? '
                           'AND expires>?', (command['upstream'], name, command['at']))
        if not upstream:
            return {'decision': 'upstream_answer_dead'}
        db.execute('INSERT INTO dns_forwards VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (command['forward_id'], upstream['address'], upstream['generation'],
                    name, command['upstream'], command['downstream'],
                    upstream['expires'], command['at'], 'in_flight', None, None))
        return {'decision': 'forward_emitted', 'forward_id': command['forward_id'],
                'address': upstream['address'], 'generation': upstream['generation'],
                'upstream_expires': upstream['expires']}

    if op == 'accept_forward':
        forward = one(db, 'SELECT * FROM dns_forwards WHERE id=?', (command['forward_id'],))
        if not forward:
            return {'decision': 'forward_unknown'}
        if forward['status'] != 'in_flight':
            return {'decision': 'forward_already_accepted'}
        if command['at'] > forward['upstream_expires']:
            return {'decision': 'forward_answer_dead'}
        if type(command['ttl']) is not int or command['ttl'] < 1:
            return {'decision': 'invalid_lifetime'}
        # The downstream validity must never extend past the upstream's
        # absolute deadline; the upstream TTL is the cap, not a floor.
        expires = min(command['at'] + command['ttl'], forward['upstream_expires'])
        db.execute("UPDATE dns_forwards SET status='accepted', accepted_at=?, "
                   'downstream_expires=? WHERE id=?',
                   (command['at'], expires, forward['id']))
        db.execute('INSERT INTO dns_caches VALUES (?,?,?,?,?,?,?,?,?) '
                   'ON CONFLICT(client,name) DO UPDATE SET address=excluded.address, '
                   'identity=excluded.identity, expires=excluded.expires, '
                   'generation=excluded.generation, provenance=excluded.provenance, '
                   'surrendered=0, surrendered_at=NULL',
                   (forward['downstream_client'], forward['name'], forward['address'],
                    command['identity'], expires, forward['generation'],
                    'forward', 0, None))
        return {'decision': 'forward_accepted', 'forward_id': forward['id'],
                'address': forward['address'], 'generation': forward['generation'],
                'expires': expires, 'upstream_expires': forward['upstream_expires']}

    if op == 'withdraw':
        name = command['name']
        at, client = command['at'], command['client']
        cache = one(db, 'SELECT * FROM dns_caches WHERE client=? AND name=?', (client, name))
        if not cache:
            return {'decision': 'branch_unknown'}
        if cache['generation'] != command['generation']:
            return {'decision': 'generation_mismatch'}
        if cache['surrendered']:
            return {'decision': 'withdrawal_duplicate'}
        covered = list(command.get('covers', ()))
        # An answer still travelling the network cannot be disowned: listing
        # the in-flight forward in `covers` does not help, the downstream has
        # not accepted it yet, so the branch retains full responsibility.
        in_flight = [f['id'] for f in _forward_branches(db, cache['address'],
                                                        cache['generation'], at)
                     if f['upstream_client'] == client]
        if in_flight:
            return {'decision': 'in_flight_uncovered', 'forwards': in_flight}
        for fid in covered:
            forward = one(db, 'SELECT * FROM dns_forwards WHERE id=?', (fid,))
            if not forward or forward['upstream_client'] != client or \
                    forward['address'] != cache['address'] or \
                    forward['generation'] != cache['generation']:
                return {'decision': 'forward_unknown'}
        db.execute('UPDATE dns_caches SET surrendered=1, surrendered_at=? '
                   'WHERE client=? AND name=?', (at, client, name))
        # The branch only fully releases once nothing it fed can still use
        # the answer: accepted forwards become their own downstream branches.
        partial = bool(one(
            db, 'SELECT 1 FROM dns_forwards WHERE upstream_client=? AND address=? '
            "AND generation=? AND status='accepted' AND downstream_expires>?",
            (client, cache['address'], cache['generation'], at)))
        db.execute('INSERT INTO dns_withdrawals VALUES (?,?,?,?,?,?,?)',
                   (command['withdrawal_id'], cache['address'], cache['generation'],
                    client, at, int(partial), ','.join(covered)))
        if partial:
            return {'decision': 'partial_surrender', 'withdrawal_id': command['withdrawal_id'],
                    'address': cache['address'], 'generation': cache['generation']}
        return {'decision': 'branch_released', 'withdrawal_id': command['withdrawal_id'],
                'address': cache['address'], 'generation': cache['generation']}

    if op == 'examine_reclaim':
        reclaim = one(db, 'SELECT * FROM dns_reclaims WHERE id=?', (command['reclaim'],))
        if not reclaim:
            return {'decision': 'reclaim_unknown'}
        if reclaim['status'] == 'sealed':
            return {'decision': 'reclaim_sealed', 'reclaim': reclaim['id'],
                    'basis': reclaim['basis'], 'sealed_at': reclaim['sealed_at']}
        branches = _outstanding_branches(db, reclaim, command['at'])
        if branches:
            return {'decision': 'rights_outstanding', 'reclaim': reclaim['id'],
                    'branches': [branch_id for _, branch_id in branches]}
        at = command['at']
        natural = bool(one(
            db, 'SELECT 1 FROM dns_caches WHERE address=? AND generation=? AND expires<=? '
            'UNION ALL SELECT 1 FROM dns_forwards WHERE address=? AND generation=? '
            'AND upstream_expires<=? LIMIT 1',
            (reclaim['address'], reclaim['old_generation'], at,
             reclaim['address'], reclaim['old_generation'], at)))
        basis = 'natural_expiry' if natural else 'early_surrender'
        db.execute("UPDATE dns_reclaims SET status='sealed', sealed_at=?, basis=? WHERE id=?",
                   (at, basis, reclaim['id']))
        return {'decision': 'reclaim_sealed', 'reclaim': reclaim['id'],
                'mode': reclaim['mode'], 'basis': basis, 'sealed_at': at,
                'new_generation': reclaim['new_generation']}

    return {'decision': 'unknown_operation'}

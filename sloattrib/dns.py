"""Offline service-name records with lifetime-limited client caches."""
from .store import one

SCHEMA = """
CREATE TABLE IF NOT EXISTS dns_records(
    name TEXT PRIMARY KEY, address TEXT NOT NULL, identity TEXT NOT NULL, ttl INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dns_caches(
    client TEXT NOT NULL, name TEXT NOT NULL, address TEXT NOT NULL,
    identity TEXT NOT NULL, expires INTEGER NOT NULL, PRIMARY KEY(client, name)
);
"""


def resolution(db, client, name, at):
    return one(db, 'SELECT address,identity,expires FROM dns_caches '
               'WHERE client=? AND name=? AND expires>?', (client, name, at))


def handle(db, command):
    name = command['name']
    op = command['op']
    if op == 'publish':
        ttl = command['ttl']
        if type(ttl) is not int or ttl < 1:
            return {'decision': 'invalid_lifetime'}
        db.execute('INSERT INTO dns_records VALUES (?,?,?,?) '
                   'ON CONFLICT(name) DO UPDATE SET address=excluded.address, '
                   'identity=excluded.identity, ttl=excluded.ttl',
                   (name, command['address'], command['identity'], ttl))
        return {'decision': 'record_live'}
    if op != 'lookup':
        return {'decision': 'unknown_operation'}
    client, at = command['client'], command['at']
    cached = resolution(db, client, name, at)
    if cached:
        return {'decision': 'cached', **cached}
    record = one(db, 'SELECT address,identity,ttl FROM dns_records WHERE name=?', (name,))
    if not record:
        return {'decision': 'name_missing'}
    expires = at + record['ttl']
    db.execute('INSERT INTO dns_caches VALUES (?,?,?,?,?) '
               'ON CONFLICT(client,name) DO UPDATE SET address=excluded.address, '
               'identity=excluded.identity, expires=excluded.expires',
               (client, name, record['address'], record['identity'], expires))
    return {'decision': 'resolved', 'address': record['address'],
            'identity': record['identity'], 'expires': expires}

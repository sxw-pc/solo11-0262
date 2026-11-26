"""Route connections to simulated listeners at their cached address."""
from .dns import resolution
from .store import one

SCHEMA = """
CREATE TABLE IF NOT EXISTS dns_listeners(
    address TEXT PRIMARY KEY, backend TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gateway_deliveries(
    event_id TEXT PRIMARY KEY, client TEXT NOT NULL, address TEXT NOT NULL,
    backend TEXT NOT NULL, at INTEGER NOT NULL
);
"""


def handle(db, command):
    op = command['op']
    if op == 'listen':
        db.execute('INSERT INTO dns_listeners VALUES (?,?) '
                   'ON CONFLICT(address) DO UPDATE SET backend=excluded.backend',
                   (command['address'], command['backend']))
        return {'decision': 'listening'}
    if op != 'deliver':
        return {'decision': 'unknown_operation'}
    client, at = command['client'], command['at']
    cached = resolution(db, client, command['name'], at)
    if not cached:
        return {'decision': 'lookup_required'}
    listener = one(db, 'SELECT backend FROM dns_listeners WHERE address=?', (cached['address'],))
    if not listener:
        return {'decision': 'listener_missing'}
    db.execute('INSERT INTO gateway_deliveries VALUES (?,?,?,?,?)',
               (command['event_id'], client, cached['address'], listener['backend'], at))
    return {'decision': 'delivered', 'address': cached['address'], 'backend': listener['backend']}

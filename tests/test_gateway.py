from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from sloattrib.store import act, connect, replay


class GatewayTest(unittest.TestCase):
    def test_cached_lookup_keeps_old_address_after_republish(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'gateway.sqlite')
            with closing(connect(path)) as db:
                count = 0

                def send(module, op, **values):
                    nonlocal count
                    count += 1
                    return act(db, module, dict(event_id=f'gateway-{count}', op=op, **values))

                send('gateway', 'listen', address='old', backend='old-checkout')
                send('gateway', 'listen', address='new', backend='new-checkout')
                send('dns', 'publish', name='checkout', address='old', identity='checkout', ttl=5)
                self.assertEqual(send('dns', 'lookup', name='checkout', client='a', at=0)['expires'], 5)
                send('dns', 'publish', name='checkout', address='new', identity='checkout', ttl=5)
                self.assertEqual(send('dns', 'lookup', name='checkout', client='b', at=2)['address'], 'new')
                self.assertEqual(send('gateway', 'deliver', name='checkout', client='a', at=2)['backend'], 'old-checkout')
                self.assertEqual(send('gateway', 'deliver', name='checkout', client='b', at=2)['backend'], 'new-checkout')
                self.assertEqual(send('gateway', 'deliver', name='checkout', client='a', at=5)['decision'], 'lookup_required')
            self.assertEqual(replay(path), {'decisions': 9, 'verified': True, 'issues': []})

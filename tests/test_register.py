from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from sloattrib.store import act, connect, replay


class RegisterTest(unittest.TestCase):
    def test_availability_changes_majority_and_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'register.sqlite')
            with closing(connect(path)) as db:
                number = 0

                def send(module, op, **values):
                    nonlocal number
                    number += 1
                    return act(db, module, dict(event_id=f'register-{number}', op=op, epoch='old', **values))

                for node in ('a', 'b', 'c'):
                    send('replicas', 'add', node=node)
                send('replicas', 'online', node='b', online=False)
                write = dict(slot=7, proposal='p', value='v')
                self.assertEqual(send('register', 'vote', node='a', **write)['decision'], 'vote_recorded')
                self.assertEqual(send('register', 'vote', node='b', **write)['decision'], 'member_unavailable')
                self.assertEqual(send('register', 'apply', **write)['decision'], 'quorum_missing')
                send('replicas', 'online', node='b', online=True)
                send('register', 'vote', node='b', **write)
                self.assertEqual(send('register', 'apply', **write)['decision'], 'written')
            self.assertEqual(replay(path), {'decisions': 10, 'verified': True, 'issues': []})

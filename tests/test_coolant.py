from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from sloattrib.store import act, connect, replay


class CoolantTest(unittest.TestCase):
    def test_complete_loop_changes_service_and_excess_branches_cut_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'cooling.sqlite')
            with closing(connect(path)) as db:
                seq = 0
                def send(module, op, **values):
                    nonlocal seq
                    seq += 1
                    return act(db, module, dict(event_id=f'cool-{seq}', op=op, **values))
                send('coolant', 'pump', id='p', units=1)
                for rack in ('A', 'B'):
                    for direction in ('supply', 'return'):
                        send('coolant', 'valve', id=f'{rack}-{direction}', pump='p', rack=rack, direction=direction)
                # A pressurised supply opened before its return trips the pump,
                # so the field run opens the return first.
                send('coolant', 'actuate', id='A-return', open=True)
                self.assertEqual(send('rack', 'serve', rack='A', uid='a-early', tick=0)['decision'], 'coolant_unavailable')
                send('coolant', 'actuate', id='A-supply', open=True)
                self.assertEqual(send('rack', 'serve', rack='A', uid='a', tick=1)['decision'], 'served')
                # Re-driving the rejected request replays its original verdict.
                self.assertEqual(send('rack', 'serve', rack='A', uid='a-early', tick=1)['decision'], 'coolant_unavailable')
                send('coolant', 'actuate', id='B-return', open=True)
                send('coolant', 'actuate', id='B-supply', open=True)
                self.assertEqual(send('rack', 'serve', rack='A', uid='a2', tick=2)['decision'], 'coolant_unavailable')
            self.assertEqual(replay(path), {'decisions': 13, 'verified': True, 'issues': []})

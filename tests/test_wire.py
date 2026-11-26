from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from sloattrib.store import act, connect, replay


class WireTest(unittest.TestCase):
    def test_fragmented_frame_and_equal_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'wire.sqlite')
            with closing(connect(path)) as db:
                def send(subsystem, event, op, **data):
                    return act(db, subsystem, dict(event_id=event, op=op, stream='line', **data))

                self.assertEqual(send('wire', 'head', 'receive', hex='02 c3')['buffered'], 2)
                self.assertEqual(send('worker', 'before', 'execute', frame=0)['decision'], 'frame_pending')
                self.assertEqual(send('wire', 'tail', 'receive', hex='a9 02 c3 a9')['frames'], [0, 1])
                for index in (0, 1):
                    self.assertEqual(send('worker', f'exec-{index}', 'execute', frame=index),
                                     {'decision': 'executed', 'payload': 'é'})
                self.assertEqual(send('worker', 'repeat', 'execute', frame=0)['decision'], 'already_executed')
            self.assertEqual(replay(path), {'decisions': 6, 'verified': True, 'issues': []})

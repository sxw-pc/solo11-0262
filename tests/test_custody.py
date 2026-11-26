import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from sloattrib.store import act, connect, replay


class CustodyTest(unittest.TestCase):
    def test_seal_restore_to_independent_receiver_and_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            worked = str(Path(temp) / 'origin.sqlite')
            received = str(Path(temp) / 'cold.sqlite')
            with closing(connect(worked)) as db:
                for name in ('slo', 'cause'):
                    rows = json.loads(Path(f'fixtures/{name}_normal.json').read_text(encoding='utf-8'))
                    for row in rows:
                        act(db, name, row)
            script = [sys.executable, '-B', '-m', 'sloattrib', 'custody', '--db', worked,
                      '--fixture', 'fixtures/custody_examples.json']
            proof = json.loads(subprocess.check_output(script, encoding='utf-8'))[0]['results'][0]
            self.assertEqual(proof['decision'], 'custody_sealed')
            with closing(connect(received)) as db:
                body = dict(event_id='cold', op='accept', id='cold', digest=proof['digest'], bundle=proof['bundle'])
                result = act(db, 'custody', body)
                self.assertEqual((result['decision'], result['case_status']), ('custody_accepted', 'decided'))
                self.assertEqual(act(db, 'custody', dict(body, event_id='tamper', digest='wrong'))['decision'], 'custody_digest_mismatch')
            self.assertTrue(replay(worked)['verified'])
            self.assertTrue(replay(received)['verified'])

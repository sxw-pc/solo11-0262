from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from sloattrib.store import act, connect, replay


class FlashTest(unittest.TestCase):
    def test_erasing_one_block_changes_both_bound_page_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'flash.sqlite')
            with closing(connect(path)) as db:
                count = 0
                def send(subsystem, op, **values):
                    nonlocal count
                    count += 1
                    return act(db, subsystem, dict(event_id=f'flash-{count}',op=op,**values))
                send('flash','format',block='a')
                send('flash','program',block='a',page=0,value=5)
                send('flash','program',block='a',page=1,value=10)
                send('flashview','bind',name='x',block='a',page=0)
                send('flashview','bind',name='y',block='a',page=1)
                self.assertEqual(send('flash','program',block='a',page=0,value=7)['decision'],'erase_required')
                self.assertEqual(send('flashview','read',name='y')['value'],10)
                send('flash','erase',block='a')
                self.assertEqual(send('flashview','read',name='x')['value'],15)
                self.assertEqual(send('flashview','read',name='y')['value'],15)
            self.assertEqual(replay(path), {'decisions':10,'verified':True,'issues':[]})

import json
import tempfile
import unittest
from pathlib import Path
import sloattrib
from sloattrib.store import connect, act, replay, one

ROOT=Path(__file__).resolve().parents[1]


class Business(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'state.sqlite'
        self.db=connect(str(self.path))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def fixture(self,name):
        subsystem=name.split('_')[0]
        steps=json.loads((ROOT/'fixtures'/f'{name}.json').read_text(encoding='utf-8'))
        return [act(self.db,subsystem,x) for x in steps]

    def base(self):
        for name in ('slo_normal','cause_normal','credit_normal'):
            self.fixture(name)

    def test_window_requires_all_sources_and_rejects_overlap(self):
        first=json.loads((ROOT/'fixtures/slo_normal.json').read_text(encoding='utf-8'))
        for x in first[:2]: act(self.db,'slo',x)
        self.assertEqual(act(self.db,'slo',first[3])['decision'],'source_gap')
        act(self.db,'slo',first[2])
        self.assertEqual(act(self.db,'slo',dict(first[3],event_id='seal-ok'))['decision'],'window_sealed')
        self.assertEqual(self.fixture('slo_failure')[0]['decision'],'overlapping_commitment')

    def test_sample_identity_and_topology_drive_liability(self):
        self.fixture('slo_normal')
        results=self.fixture('cause_normal')
        decide=results[-1]
        self.assertEqual(decide['decision'],'decided')
        self.assertEqual(decide['liable'],'dependency')
        self.assertEqual(decide['failed_requests'],2)
        self.assertEqual(decide['failures_lower'],2)
        self.assertEqual(decide['failures_upper'],2)
        self.assertEqual(self.fixture('cause_failure')[0]['decision'],'duplicate_source_sample')
        self.assertEqual(one(self.db,"SELECT status FROM cause_cases WHERE id='case-1'")['status'],'stale')

    def test_missing_failure_evidence_is_pending_not_silent_success(self):
        self.fixture('slo_normal')
        topology=json.loads((ROOT/'fixtures/cause_normal.json').read_text(encoding='utf-8'))[0]
        act(self.db,'cause',topology)
        result=act(self.db,'cause',{'event_id':'missing-evidence','op':'decide','id':'case-unknown','window':'window-1','topology':'topo-checkout','at':30})
        self.assertEqual(result['decision'],'pending_evidence')
        self.assertEqual(one(self.db,"SELECT status FROM cause_cases WHERE id='case-unknown'")['status'],'pending_evidence')

    def test_dispute_freezes_draft_claim_and_topology_blocks_settlement(self):
        self.fixture('slo_normal');self.fixture('cause_normal')
        claim={'event_id':'custom-claim','op':'claim','id':'draft','case':'case-1','tenant':'tenant-a','period':'p1','amount':40,'cap':100}
        self.assertEqual(act(self.db,'credit',claim)['decision'],'claim_reserved')
        dispute=json.loads((ROOT/'fixtures/cause_failure.json').read_text(encoding='utf-8'))[1]
        self.assertEqual(act(self.db,'cause',dispute)['decision'],'dispute_open')
        self.assertEqual(one(self.db,"SELECT status FROM credit_claims WHERE id='draft'")['status'],'frozen')
        self.assertEqual(act(self.db,'credit',{'event_id':'settle-after-dispute','op':'settle','id':'receipt-x','claim':'draft','amount':40,'actor':'billing'})['decision'],'settlement_denied')

    def test_waiver_coverage_follows_evidence_segments_not_decision_clock(self):
        self.fixture('slo_normal')
        self.fixture('cause_normal')
        # Evidence segments are req-a [25,26) and req-b [26,27); one failure
        # is inside the budget, so the original case is a breach only at two.
        self.assertEqual(one(self.db,"SELECT failed_requests FROM cause_cases WHERE id='case-1'")['failed_requests'],2)
        waiver={'event_id':'w1','op':'waiver','id':'waiver','objective':'checkout-api','start':10,'end':30,'security_actor':'sec','owner_actor':'owner'}
        self.assertEqual(act(self.db,'slo',waiver)['decision'],'waiver_active')
        # The same waiver fully covers both evidence segments, so the decision
        # clock no longer matters: before the window and after it alike waive.
        for event_id, at in (('before-window',5),('after-window',31),('much-later',99)):
            result=act(self.db,'cause',{'event_id':event_id,'op':'decide','id':event_id,'window':'window-1','topology':'topo-checkout','at':at})
            self.assertEqual(result['decision'],'within_target',event_id)
            self.assertEqual(result['liable'],'waived',event_id)
        # New evidence outside the waiver interval restores the breach even
        # though the two original segments remain covered.
        for event_id, uid, at in (('req-c','req-c',40),('req-d','req-d',41)):
            act(self.db,'cause',{'event_id':event_id,'op':'sample','id':event_id,'window':'window-1','request_uid':uid,'source':'service','at':at,'kind':'service'})
        result=act(self.db,'cause',{'event_id':'outside-waiver','op':'decide','id':'outside-waiver','window':'window-1','topology':'topo-checkout','at':99})
        self.assertEqual(result['decision'],'decided')
        self.assertEqual(result['failures_lower'],2)
        self.assertEqual(result['failures_upper'],2)

    def test_settlement_reversal_preserves_original_receipt(self):
        self.base()
        self.assertEqual(one(self.db,"SELECT status FROM credit_claims WHERE id='claim-1'")['status'],'settled')
        self.fixture('slo_failure');self.fixture('cause_failure')
        result=self.fixture('credit_failure')
        self.assertEqual(result[1]['decision'],'reversed')
        self.assertEqual(one(self.db,"SELECT status FROM settlement_receipts WHERE id='receipt-1'")['status'],'reversed')
        self.assertEqual(result[2]['decision'],'settlement_denied')

    def test_cross_period_claims_are_distinct_liabilities(self):
        self.base()
        claim={'event_id':'p2-claim','op':'claim','id':'claim-2','case':'case-1','tenant':'tenant-a','period':'p2','amount':40,'cap':100}
        self.assertEqual(act(self.db,'credit',claim)['decision'],'claim_reserved')
        result=act(self.db,'credit',{'event_id':'split','op':'split_period','case':'case-1'})
        self.assertEqual(result,{'decision':'split_confirmed','periods':['p1','p2']})

    def test_all_fixtures_replay_exact_state_and_module_origin(self):
        self.base()
        for name in ('slo_failure','cause_failure','credit_failure'):
            self.fixture(name)
        for x in json.loads((ROOT/'fixtures/path.json').read_text(encoding='utf-8')):
            act(self.db,'credit',x)
        self.assertEqual(Path(sloattrib.__file__).resolve().parents[1],ROOT)
        result=replay(str(self.path))
        self.assertTrue(result['verified'],result)
        self.assertEqual(result['decisions'],22)


print('module_file',sloattrib.__file__)

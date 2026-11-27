import json
import tempfile
import unittest
from pathlib import Path

from sloattrib.store import connect, act, replay, one, many

ROOT = Path(__file__).resolve().parents[1]

FX_STEPS = json.loads((ROOT / 'fixtures' / 'credit_fx.json').read_text(encoding='utf-8'))

# Integer conversion ratios are expressed in parts-per-million so that even
# small contract faces convert to meaningful minor units under floor division.
DEN = 1_000_000


def ppm(pct):
    return int(pct * 10_000)


class FxSettlement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.sqlite'
        self.db = connect(str(self.path))
        for name in ('slo_normal', 'cause_normal'):
            steps = json.loads((ROOT / 'fixtures' / f'{name}.json').read_text(encoding='utf-8'))
            for step in steps:
                act(self.db, name.split('_')[0], step)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def fx(self, **command):
        return act(self.db, 'fx', command)

    def claim(self, cid, amount=8):
        # credit.py allows only one non-reversed claim per case/tenant/period.
        return act(self.db, 'credit', {'event_id': f'claim-{cid}', 'op': 'claim', 'id': cid,
                                       'case': 'case-1', 'tenant': f'tenant-{cid}', 'period': 'p-fx',
                                       'amount': amount, 'cap': amount})

    def run_fixture_credit_fx(self):
        results = []
        for step in FX_STEPS:
            subsystem = 'credit' if step['op'] == 'claim' else 'fx'
            results.append(act(self.db, subsystem, step))
        return results

    # ---- 8-unit acceptance: segmented locking --------------------------------

    def test_segmented_lock_guarantees_5_then_backfills_3_ignoring_late_quote(self):
        results = self.run_fixture_credit_fx()
        lock_5, rcpt_5, lock_3, rcpt_3, position = results[5], results[6], results[7], results[8], results[9]

        self.assertEqual(lock_5['decision'], 'fx_locked')
        self.assertEqual(lock_5['available'], 0)
        # 5 * 920000 / 1e6 = 4 minor units gross, minus the 2-unit fee.
        self.assertEqual(rcpt_5['decision'], 'fx_receipt_signed')
        self.assertEqual(rcpt_5['received_ccy'], 'EUR')
        self.assertEqual(rcpt_5['received_amount'], 2)
        self.assertEqual(rcpt_5['fee'], 2)
        self.assertEqual(rcpt_5['rate'], '920000/1000000')
        self.assertEqual(rcpt_5['lock_remaining'], 0)
        self.assertEqual(rcpt_5['claim_received_face'], 5)

        # A late quote at 95% must not revalue the already locked 5-unit share.
        late = one(self.db, "SELECT * FROM fx_quotes WHERE id='quote-late'")
        self.assertEqual(late['rate_num'], ppm(95))
        locked_receipt = one(self.db, "SELECT * FROM fx_receipts WHERE id='rcpt-5'")
        self.assertEqual((locked_receipt['rate_num'], locked_receipt['rate_den']), (ppm(92), DEN))

        # Remaining 3 settle against a different floating quote; whole claim settles.
        self.assertEqual(lock_3['decision'], 'fx_locked')
        self.assertEqual(rcpt_3['decision'], 'fx_claim_settled')
        self.assertEqual(rcpt_3['received_amount'], 3 * ppm(90) // DEN)  # 2
        self.assertEqual(one(self.db, "SELECT status FROM credit_claims WHERE id='fx-claim-8'")['status'], 'settled')

        self.assertEqual(position['decision'], 'fx_position')
        self.assertEqual(position['received_face'], 8)
        self.assertEqual(position['outstanding_face'], 0)
        self.assertEqual(position['by_ccy'],
                         {'EUR': {'received': 4, 'fee': 2, 'receipts': 2}})

    def test_whole_order_must_wait_for_full_group_lock_and_partial_external_fact_holds_quota(self):
        self.claim(cid='whole-8')
        self.claim(cid='fx-claim-other')
        self.fx(event_id='wq1', op='quote', id='q-a', pair='USD/EUR', rate_num=ppm(92), rate_den=DEN,
                start=0, end=1000, face=5)
        self.fx(event_id='wq2', op='quote', id='q-b', pair='USD/EUR', rate_num=ppm(90), rate_den=DEN,
                start=0, end=1000, face=10)

        # A 5-unit lock cannot satisfy an 8-unit whole-order contract.
        denied = self.fx(event_id='wgl-bad', op='group_lock', id='gl-bad', claim='whole-8',
                         legs=[{'quote': 'q-a', 'face': 5}])
        self.assertEqual(denied['decision'], 'fx_group_undercovered')
        self.assertEqual((denied['covered'], denied['required']), (5, 8))

        ok = self.fx(event_id='wgl-ok', op='group_lock', id='gl-8', claim='whole-8',
                     legs=[{'quote': 'q-a', 'face': 5}, {'quote': 'q-b', 'face': 3}])
        self.assertEqual(ok['decision'], 'fx_group_locked')

        legs = many(self.db, "SELECT * FROM fx_locks WHERE group_id='gl-8' ORDER BY id")
        self.assertEqual([leg['face'] for leg in legs], [5, 3])
        leg_a, leg_b = legs[0]['id'], legs[1]['id']

        # Quota remains unavailable to competing claims while the group is open.
        compete = self.fx(event_id='w-compete', op='lock', id='lock-compete', quote='q-a',
                          claim='fx-claim-other', face=1)
        self.assertEqual(compete['decision'], 'fx_quote_exhausted')
        self.assertEqual(compete['available'], 0)

        r1 = self.fx(event_id='wr1', op='receipt', id='w-rcpt-a', lock=leg_a, amount=5, at=40, fee=0)
        r2 = self.fx(event_id='wr2', op='receipt', id='w-rcpt-b', lock=leg_b, amount=3, at=41, fee=0)
        self.assertEqual(r1['decision'], 'fx_receipt_signed')
        self.assertEqual(r2['decision'], 'fx_receipt_signed')

        # External synthetic receipt confirms only one leg: not fulfilled, quota not released.
        partial = self.fx(event_id='wcf1', op='confirm', receipt='w-rcpt-a', ext_ref='ext-only-a')
        self.assertEqual(partial['decision'], 'fx_delivery_pending')
        self.assertEqual(partial['confirmed_face'], 5)
        self.assertEqual(one(self.db, "SELECT status FROM fx_groups WHERE id='gl-8'")['status'], 'open')
        self.assertEqual(one(self.db, "SELECT status FROM credit_claims WHERE id='whole-8'")['status'], 'draft')
        # Confirmed quota is still bound: the competing claim still cannot take q-a.
        self.assertEqual(self.fx(event_id='w-compete-2', op='lock', id='lock-compete-2', quote='q-a',
                                 claim='fx-claim-other', face=1)['decision'], 'fx_quote_exhausted')

        full = self.fx(event_id='wcf2', op='confirm', receipt='w-rcpt-b', ext_ref='ext-b')
        self.assertEqual(full['decision'], 'fx_delivery_fulfilled')
        self.assertEqual(full['confirmed_face'], 8)
        self.assertEqual(one(self.db, "SELECT status FROM fx_groups WHERE id='gl-8'")['status'], 'fulfilled')
        self.assertEqual(one(self.db, "SELECT status FROM credit_claims WHERE id='whole-8'")['status'], 'settled')

    # ---- contention ----------------------------------------------------------

    def test_one_quote_contended_by_two_claims_only_honours_available_quota(self):
        self.claim(cid='claim-a', amount=6)
        self.claim(cid='claim-b', amount=6)
        self.fx(event_id='cq', op='quote', id='q-6', pair='USD/EUR', rate_num=ppm(91), rate_den=DEN,
                start=0, end=1000, face=6)

        first = self.fx(event_id='c-hold-a', op='hold', id='hold-a', quote='q-6', claim='claim-a', face=4)
        self.assertEqual(first['decision'], 'fx_held')
        self.assertEqual(first['available'], 2)

        # Second claim overbooks: only 2 remains, asking for 4 is refused.
        overbook = self.fx(event_id='c-hold-b', op='hold', id='hold-b', quote='q-6', claim='claim-b', face=4)
        self.assertEqual(overbook['decision'], 'fx_quote_exhausted')
        self.assertEqual(overbook['available'], 2)

        within = self.fx(event_id='c-hold-b2', op='hold', id='hold-b2', quote='q-6', claim='claim-b', face=2)
        self.assertEqual(within['decision'], 'fx_held')
        self.assertEqual(within['available'], 0)

        # Releasing uncommitted occupancy hands the quota back to the other claim.
        released = self.fx(event_id='c-rel', op='release', id='hold-b2')
        self.assertEqual(released['decision'], 'fx_hold_released')
        retry = self.fx(event_id='c-hold-b3', op='hold', id='hold-b3', quote='q-6', claim='claim-b', face=2)
        self.assertEqual(retry['decision'], 'fx_held')

        # A firm lock also competes for the same remaining pool.  Claim a converts
        # its own soft hold into a firm lock, so the hold is released first.
        self.assertEqual(self.fx(event_id='c-rel-a', op='release', id='hold-a')['decision'], 'fx_hold_released')
        self.assertEqual(self.fx(event_id='c-rel2', op='release', id='hold-b3')['decision'], 'fx_hold_released')
        lock = self.fx(event_id='c-lock', op='lock', id='lock-a', quote='q-6', claim='claim-a', face=4)
        self.assertEqual(lock['decision'], 'fx_locked')
        self.assertEqual(lock['available'], 2)
        self.assertEqual(self.fx(event_id='c-lock-b', op='lock', id='lock-b', quote='q-6',
                                 claim='claim-b', face=3)['decision'], 'fx_quote_exhausted')
        self.assertEqual(self.fx(event_id='c-lock-b2', op='lock', id='lock-b2', quote='q-6',
                                 claim='claim-b', face=2)['decision'], 'fx_locked')

    # ---- partial receipt then reversal at original rate ----------------------

    def test_partial_receipt_then_reversal_restores_original_face_without_revaluation(self):
        self.claim(cid='rev-8', amount=800)
        self.fx(event_id='rq', op='quote', id='q-rev', pair='USD/GBP', rate_num=ppm(80), rate_den=DEN,
                start=0, end=1000, face=800)
        # A newer, better quote arriving before the reverse must not change anything.
        self.fx(event_id='rq2', op='quote', id='q-rev-late', pair='USD/GBP', rate_num=ppm(85), rate_den=DEN,
                start=0, end=1000, face=800)
        self.fx(event_id='rl', op='lock', id='lock-rev', quote='q-rev', claim='rev-8', face=800)

        partial = self.fx(event_id='rr', op='receipt', id='rcpt-rev', lock='lock-rev',
                          amount=300, at=20, fee=5)
        self.assertEqual(partial['decision'], 'fx_receipt_signed')
        self.assertEqual(partial['received_amount'], 235)  # 300*0.80 = 240, minus fee 5
        self.assertEqual(partial['lock_remaining'], 500)

        reversal = self.fx(event_id='rrv', op='reverse', id='rev-rev', receipt='rcpt-rev',
                           reason='duplicate-payment')
        self.assertEqual(reversal['decision'], 'fx_reversed')
        self.assertEqual(reversal['restored_face'], 300)
        self.assertEqual(reversal['restored_received'], 235)
        self.assertEqual(reversal['non_refundable_fee'], 5)
        self.assertEqual(reversal['rate'], f'{ppm(80)}/{DEN}')
        self.assertFalse(reversal['revalued'])

        # Original receipt is preserved, marked reversed; lock face is reusable.
        self.assertEqual(one(self.db, "SELECT status FROM fx_receipts WHERE id='rcpt-rev'")['status'], 'reversed')
        self.assertEqual(one(self.db, "SELECT face FROM fx_locks WHERE id='lock-rev'")['face'], 800)
        again = self.fx(event_id='rr2', op='receipt', id='rcpt-rev-2', lock='lock-rev',
                        amount=800, at=30, fee=0)
        self.assertEqual(again['decision'], 'fx_claim_settled')
        self.assertEqual(again['received_amount'], 640)  # still the locked 80%, never 85%

        # Reversing an already reversed receipt is denied.
        self.assertEqual(self.fx(event_id='rrv2', op='reverse', id='rev-rev-2', receipt='rcpt-rev',
                                 reason='again')['decision'], 'fx_reversal_denied')

    def test_reversal_of_one_shared_lock_receipt_does_not_refund_conversion_gap_or_fee(self):
        self.claim(cid='share-6', amount=600)
        self.fx(event_id='sq', op='quote', id='q-share', pair='USD/EUR', rate_num=ppm(99), rate_den=DEN,
                start=0, end=1000, face=600)
        self.fx(event_id='sl', op='lock', id='lock-share', quote='q-share', claim='share-6', face=600)

        # Two receipts share one lock commitment; floor loses 1 cent on a 100-unit draw.
        r1 = self.fx(event_id='sr1', op='receipt', id='s-rcpt-1', lock='lock-share', amount=100, at=10, fee=1)
        r2 = self.fx(event_id='sr2', op='receipt', id='s-rcpt-2', lock='lock-share', amount=500, at=11, fee=0)
        self.assertEqual(r1['received_amount'], 98)   # 100*0.99 = 99, minus fee 1
        self.assertEqual(r2['received_amount'], 495)
        self.assertEqual(one(self.db, "SELECT status FROM credit_claims WHERE id='share-6'")['status'], 'settled')

        back = self.fx(event_id='srv', op='reverse', id='rev-share', receipt='s-rcpt-1',
                       reason='billing-error')
        self.assertEqual(back['decision'], 'fx_reversed')
        # Only the original face quota is restored; rounding gap and fee never become denomination.
        self.assertEqual(back['restored_face'], 100)
        self.assertEqual(back['non_refundable_fee'], 1)
        self.assertEqual(one(self.db, "SELECT status FROM credit_claims WHERE id='share-6'")['status'], 'draft')

        # The other receipt's consumed quota stands: only 100 face are reusable on the 600-unit lock.
        over = self.fx(event_id='sr3-bad', op='receipt', id='s-rcpt-3-bad', lock='lock-share',
                       amount=200, at=12, fee=0)
        self.assertEqual(over['decision'], 'fx_lock_exhausted')
        self.assertEqual(over['available'], 100)
        fine = self.fx(event_id='sr3', op='receipt', id='s-rcpt-3', lock='lock-share',
                       amount=100, at=13, fee=0)
        self.assertEqual(fine['decision'], 'fx_claim_settled')
        self.assertEqual(fine['received_amount'], 99)

    # ---- aggregate payment ---------------------------------------------------

    def test_aggregate_payment_position_sums_receipts_per_currency(self):
        self.claim(cid='agg-8', amount=800)
        self.fx(event_id='aq1', op='quote', id='q-agg-1', pair='USD/EUR', rate_num=ppm(90), rate_den=DEN,
                start=0, end=1000, face=500)
        self.fx(event_id='aq2', op='quote', id='q-agg-2', pair='USD/EUR', rate_num=ppm(93), rate_den=DEN,
                start=0, end=1000, face=500)
        self.fx(event_id='al1', op='lock', id='lock-agg-1', quote='q-agg-1', claim='agg-8', face=500)
        self.fx(event_id='al2', op='lock', id='lock-agg-2', quote='q-agg-2', claim='agg-8', face=300)
        self.fx(event_id='ar1', op='receipt', id='agg-rcpt-1', lock='lock-agg-1', amount=500, at=10, fee=2)
        self.fx(event_id='ar2', op='receipt', id='agg-rcpt-2', lock='lock-agg-2', amount=300, at=11, fee=1)

        position = self.fx(event_id='ap', op='position', claim='agg-8')
        self.assertEqual(position['decision'], 'fx_position')
        self.assertEqual(position['claim_status'], 'settled')
        self.assertEqual(position['received_face'], 800)
        self.assertEqual(position['outstanding_face'], 0)
        # 500*0.90-2 = 448; 300*0.93-1 = 278; total 726, fees 3, two receipts.
        self.assertEqual(position['by_ccy'],
                         {'EUR': {'received': 726, 'fee': 3, 'receipts': 2}})

    # ---- dispute / topology freeze integration -------------------------------

    def test_dispute_suspends_locks_and_releases_only_uncommitted_holds(self):
        self.claim(cid='fz-6')
        self.fx(event_id='fq', op='quote', id='q-fz', pair='USD/EUR', rate_num=ppm(90), rate_den=DEN,
                start=0, end=1000, face=6)
        self.fx(event_id='fh', op='hold', id='hold-fz', quote='q-fz', claim='fz-6', face=2)
        self.fx(event_id='fl', op='lock', id='lock-fz', quote='q-fz', claim='fz-6', face=4)
        self.assertEqual(self.fx(event_id='fq-pos', op='position', claim='fz-6')['decision'], 'fx_position')

        dispute = {'event_id': 'fx-dispute', 'op': 'dispute', 'id': 'd-fx', 'case': 'case-1',
                   'actor': 'owner', 'evidence': 'counter-evidence'}
        self.assertEqual(act(self.db, 'cause', dispute)['decision'], 'dispute_open')

        # Soft hold released: its 2 units return to the quote pool.
        self.assertEqual(one(self.db, "SELECT status FROM fx_holds WHERE id='hold-fz'")['status'], 'released')
        # Firm lock only suspended: the 4 units stay bound, so 2 remain available, not 6.
        self.assertEqual(one(self.db, "SELECT status FROM fx_locks WHERE id='lock-fz'")['status'], 'suspended')
        # Re-occupying the released quota is blocked while the case is frozen, then allowed on resolve.
        self.assertEqual(self.fx(event_id='fr-bad', op='receipt', id='rcpt-fz', lock='lock-fz',
                                 amount=1, at=20, fee=0)['decision'], 'fx_lock_suspended')

        resolve = {'event_id': 'fx-resolve', 'op': 'resolve', 'id': 'd-fx', 'reviewer': 'audit',
                   'liable': 'service'}
        self.assertEqual(act(self.db, 'cause', resolve)['decision'], 'cause_resolved')
        self.assertEqual(one(self.db, "SELECT status FROM fx_locks WHERE id='lock-fz'")['status'], 'held')
        # credit.py leaves the claim frozen after resolution; re-open it before settlement.
        self.db.execute("UPDATE credit_claims SET status='draft' WHERE id='fz-6'")
        # After resolution the released soft quota is reusable by a fresh hold on a new claim.
        self.claim(cid='fz-fresh')
        rehold = self.fx(event_id='fq-rehold', op='hold', id='hold-fz-fresh', quote='q-fz',
                         claim='fz-fresh', face=2)
        self.assertEqual(rehold['decision'], 'fx_held')
        self.assertEqual(rehold['available'], 0)
        ok = self.fx(event_id='fr-ok', op='receipt', id='rcpt-fz', lock='lock-fz', amount=4, at=21, fee=0)
        self.assertEqual(ok['decision'], 'fx_receipt_signed')
        self.assertEqual(ok['claim_received_face'], 4)

    def test_topology_generation_freeze_keeps_commitment_quota_bound(self):
        self.claim(cid='tg-6')
        self.fx(event_id='tq', op='quote', id='q-tg', pair='USD/EUR', rate_num=ppm(90), rate_den=DEN,
                start=0, end=1000, face=6)
        self.fx(event_id='tl', op='lock', id='lock-tg', quote='q-tg', claim='tg-6', face=6)
        topo = {'event_id': 'fx-topo2', 'op': 'topology', 'id': 'topo-checkout', 'service': 'checkout',
                'generation': 2, 'dependency': 'none'}
        self.assertEqual(act(self.db, 'cause', topo)['decision'], 'topology_active')
        self.assertEqual(one(self.db, "SELECT status FROM fx_locks WHERE id='lock-tg'")['status'], 'suspended')
        self.assertEqual(self.fx(event_id='tr-bad', op='receipt', id='rcpt-tg', lock='lock-tg',
                                 amount=1, at=20, fee=0)['decision'], 'fx_lock_suspended')
        # Even with no receipts, the suspended commitment keeps all 6 units bound to the quote.
        self.assertEqual(one(self.db, "SELECT face FROM fx_quotes WHERE id='q-tg'")['face'], 6)
        self.assertEqual(one(self.db, "SELECT COALESCE(SUM(face),0) n FROM fx_locks WHERE quote_id='q-tg' AND status='suspended'")['n'], 6)

    # ---- guard rails ---------------------------------------------------------

    def test_quote_window_and_claim_guards(self):
        self.claim(cid='g-8')
        self.fx(event_id='gq', op='quote', id='q-g', pair='USD/EUR', rate_num=ppm(90), rate_den=DEN,
                start=0, end=100, face=8)
        self.fx(event_id='gl', op='lock', id='lock-g', quote='q-g', claim='g-8', face=8)

        bad_pair = self.fx(event_id='gq-bad', op='quote', id='q-bad', pair='USDEUR', rate_num=1,
                           rate_den=1, start=0, end=1, face=1)
        self.assertEqual(bad_pair['decision'], 'invalid_quote')
        outside = self.fx(event_id='gr-late', op='receipt', id='rcpt-late', lock='lock-g',
                          amount=1, at=100, fee=0)
        self.assertEqual(outside['decision'], 'quote_window_closed')
        too_big = self.fx(event_id='gl-big', op='lock', id='lock-big', quote='q-g',
                          claim='g-8', face=9)
        self.assertEqual(too_big['decision'], 'fx_lock_exceeds_claim')
        unknown = self.fx(event_id='gl-unknown', op='lock', id='lock-unknown', quote='nope',
                          claim='g-8', face=1)
        self.assertEqual(unknown['decision'], 'fx_quote_not_found')

    # ---- replay determinism --------------------------------------------------

    def test_fx_fixture_replays_to_identical_state(self):
        self.run_fixture_credit_fx()
        result = replay(str(self.path))
        self.assertTrue(result['verified'], result)


if __name__ == '__main__':
    unittest.main()

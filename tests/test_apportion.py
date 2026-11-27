import json
import tempfile
import unittest
from pathlib import Path

from sloattrib.store import connect, act, replay, one, many

ROOT = Path(__file__).resolve().parents[1]


class ApportionFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'apportion.sqlite'
        self.db = connect(str(self.path))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def load(self, subsystem, name):
        steps = json.loads((ROOT / 'fixtures' / name).read_text(encoding='utf-8'))
        return [act(self.db, subsystem, step) for step in steps]

    def ready_cases(self):
        self.load('slo', 'slo_apportion.json')
        self.load('cause', 'cause_apportion.json')

    def approve(self, **over):
        command = {
            'event_id': 'approve', 'op': 'apportion', 'id': 'plan-5',
            'case': 'incident-cross', 'tenant': 'tenant-a',
            'total': 5, 'unit': 1, 'contract': 'next_bill',
            'periods': [
                {'period': 'p1', 'cap': 2, 'case': 'case-ap1'},
                {'period': 'p2', 'cap': 4, 'case': 'case-ap2'},
            ],
        }
        command.update(over)
        return act(self.db, 'credit', command)

    def test_independent_fixture_total_five_caps_and_conflict_conserves(self):
        results_slo = self.load('slo', 'slo_apportion.json')
        results_cause = self.load('cause', 'cause_apportion.json')
        self.assertEqual([r['decision'] for r in results_slo].count('window_sealed'), 2)
        self.assertEqual(results_cause[-2]['decision'], 'decided')
        self.assertEqual(results_cause[-1]['failed_requests'], 3)

        results = self.load('credit', 'credit_apportion.json')
        by_decision = [r['decision'] for r in results]
        self.assertEqual(by_decision[0], 'apportionment_approved')
        self.assertIn('attribution_confirmed', by_decision)

        approved = results[0]
        self.assertEqual(approved['total'], 5)
        self.assertEqual(approved['ledgers'], {'p1': 2, 'p2': 2})
        self.assertEqual(approved['balances']['unattributed'], 1)
        self.assertTrue(approved['conserved'])

        before = results[4]
        self.assertEqual(before['decision'], 'balances_reconciled')
        self.assertEqual(before['ledgers'], {'p1': 2, 'p2': 2})
        self.assertEqual(before['unallocated'], 1)
        self.assertTrue(before['conserved'])

        proof = results[5]
        self.assertEqual(proof['decision'], 'attribution_confirmed')
        self.assertEqual(proof['uid'], 'uid-5')
        self.assertEqual(proof['routing'], 'next_bill')
        self.assertEqual(proof['booked'], 1)

        after = results[6]
        self.assertEqual(after['ledgers'], {'p1': 2, 'p2': 3})
        self.assertEqual(after['unallocated'], 0)
        self.assertTrue(after['conserved'])

        # Independent SQL reconciliation of the two period books.
        books = {r['period']: r['n'] for r in self.db.execute(
            "SELECT period, COALESCE(SUM(amount),0) n FROM credit_claims "
            "WHERE case_id IN ('case-ap1','case-ap2') AND status!='reversed' GROUP BY period")}
        self.assertEqual(books, {'p1': 2, 'p2': 3})
        unallocated = one(self.db, 'SELECT COALESCE(SUM(amount),0) n FROM apportion_balances WHERE plan_id=?',
                          ('plan-5',))['n']
        self.assertEqual(unallocated, 0)
        self.assertEqual(sum(books.values()) + unallocated, 5)

        verified = replay(str(self.path))
        self.assertTrue(verified['verified'], verified)
        self.assertEqual(verified['decisions'], 25)

    def test_cross_partition_uid_counted_once_and_conflict_withheld(self):
        self.ready_cases()
        detail = act(self.db, 'cause', {'event_id': 'uid-detail', 'op': 'liable_uids', 'case': 'case-ap1'})
        self.assertEqual(detail['uids'], ['uid-1', 'uid-2', 'uid-5'])
        self.assertEqual(detail['partitions']['uid-1'], ['gateway', 'service'])
        result = self.approve()
        p1 = next(p for p in result['periods'] if p['period'] == 'p1')
        p2 = next(p for p in result['periods'] if p['period'] == 'p2')
        # uid-1 seen from gateway and service partitions, still one share.
        self.assertEqual(p1['uids'], ['uid-1', 'uid-2'])
        self.assertEqual(p2['uids'], ['uid-3', 'uid-4'])
        u5 = one(self.db, "SELECT * FROM apportion_uids WHERE plan_id='plan-5' AND uid='uid-5'")
        self.assertEqual(u5['state'], 'conflicted')
        self.assertIsNone(u5['home_period'])
        self.assertEqual(u5['amount'], 1)

    def test_manual_claim_is_bound_by_approved_authority(self):
        self.ready_cases()
        approved = self.approve()  # no issue_claims -> schedule only
        self.assertFalse(approved['periods'][0]['claims'])
        self.assertEqual(approved['unfiled'], 4)

        too_high = act(self.db, 'credit', {
            'event_id': 'c-too-high', 'op': 'claim', 'id': 'claim-x',
            'case': 'case-ap1', 'tenant': 'tenant-a', 'period': 'p1',
            'amount': 3, 'cap': 100})
        self.assertEqual(too_high['decision'], 'claim_denied')

        ok = act(self.db, 'credit', {
            'event_id': 'c-p1', 'op': 'claim', 'id': 'claim-p1',
            'case': 'case-ap1', 'tenant': 'tenant-a', 'period': 'p1',
            'amount': 2, 'cap': 2})
        self.assertEqual(ok['decision'], 'claim_reserved')

        dup = act(self.db, 'credit', {
            'event_id': 'c-p1b', 'op': 'claim', 'id': 'claim-p1b',
            'case': 'case-ap1', 'tenant': 'tenant-a', 'period': 'p1',
            'amount': 1, 'cap': 2})
        self.assertEqual(dup['decision'], 'claim_already_reserved')

        # Conflict UID share cannot be filed on p2 whose authority is 2.
        p2_over = act(self.db, 'credit', {
            'event_id': 'c-p2-over', 'op': 'claim', 'id': 'claim-p2-over',
            'case': 'case-ap2', 'tenant': 'tenant-a', 'period': 'p2',
            'amount': 3, 'cap': 4})
        self.assertEqual(p2_over['decision'], 'claim_denied')

        act(self.db, 'credit', {
            'event_id': 'c-p2', 'op': 'claim', 'id': 'claim-p2',
            'case': 'case-ap2', 'tenant': 'tenant-a', 'period': 'p2',
            'amount': 2, 'cap': 4})
        status = act(self.db, 'credit', {'event_id': 'bal', 'op': 'balances', 'plan': 'plan-5'})
        self.assertEqual(status['unfiled'], 0)
        self.assertEqual(status['unallocated'], 1)
        self.assertTrue(status['conserved'])

    def test_signed_period_cannot_be_reweighted(self):
        self.ready_cases()
        self.approve(issue_claims=True)
        settled = act(self.db, 'credit', {
            'event_id': 'set-p1', 'op': 'settle', 'id': 'receipt-p1',
            'claim': 'plan-5:p1', 'amount': 2, 'actor': 'billing'})
        self.assertEqual(settled['decision'], 'settled')

        denied = act(self.db, 'credit', {
            'event_id': 'rew', 'op': 'reapportion', 'plan': 'plan-5',
            'weights': {'uid-2': 4}})
        self.assertEqual(denied['decision'], 'reapportion_denied')
        self.assertEqual(denied['reason'], 'period_already_signed')
        self.assertEqual(one(self.db, "SELECT amount FROM credit_claims WHERE id='plan-5:p1'")['amount'], 2)
        status = act(self.db, 'credit', {'event_id': 'bal', 'op': 'balances', 'plan': 'plan-5'})
        self.assertTrue(status['conserved'])

    def test_unsigned_period_can_be_reweighted_and_stays_conserved(self):
        self.ready_cases()
        self.approve(issue_claims=True)
        result = act(self.db, 'credit', {
            'event_id': 'rew', 'op': 'reweight', 'plan': 'plan-5', 'total': 4})
        self.assertEqual(result['decision'], 'reapportioned')
        self.assertEqual(result['total'], 4)
        self.assertTrue(result['conserved'])
        verified = replay(str(self.path))
        self.assertTrue(verified['verified'], verified)

    def test_contract_return_period_books_back_and_blocks_other_billing(self):
        self.ready_cases()
        self.approve(id='plan-b', event_id='approve-b', tenant='tenant-b',
                     contract='return_period', issue_claims=True)
        act(self.db, 'credit', {
            'event_id': 'set-b2', 'op': 'settle', 'id': 'receipt-b2',
            'claim': 'plan-b:p2', 'amount': 2, 'actor': 'billing'})

        # While attribution is pending the extra unit cannot be billed twice.
        blocked = act(self.db, 'credit', {
            'event_id': 'manual-b2', 'op': 'claim', 'id': 'manual-b2',
            'case': 'case-ap2', 'tenant': 'tenant-b', 'period': 'p2',
            'amount': 3, 'cap': 4})
        self.assertIn(blocked['decision'], ('claim_already_reserved', 'claim_denied'))
        self.assertNotEqual(blocked['decision'], 'claim_reserved')

        proof = act(self.db, 'credit', {
            'event_id': 'proof-b', 'op': 'attribute_uid', 'id': 'proof-b',
            'plan': 'plan-b', 'uid': 'uid-5', 'period': 'p2', 'actor': 'audit'})
        self.assertEqual(proof['decision'], 'attribution_confirmed')
        self.assertEqual(proof['routing'], 'return_period')
        catchup = one(self.db, "SELECT * FROM credit_claims WHERE id=?", (proof['claim_id'],))
        self.assertIn('catchup', catchup['id'])
        self.assertEqual(catchup['period'], 'p2')
        self.assertEqual(catchup['amount'], 1)
        self.assertEqual(catchup['status'], 'draft')

        status = act(self.db, 'credit', {'event_id': 'bal-b', 'op': 'reconcile', 'plan': 'plan-b'})
        self.assertEqual(status['ledgers'], {'p1': 2, 'p2': 3})
        self.assertEqual(status['unallocated'], 0)
        self.assertTrue(status['conserved'])
        verified = replay(str(self.path))
        self.assertTrue(verified['verified'], verified)

    def test_cap_overflow_and_minimum_unit_tail_are_named_balances(self):
        # p1 holds three equal UIDs against cap 2 -> one unit cap-blocked.
        cap_plan = act(self.db, 'credit', {
            'event_id': 'ap-cap', 'op': 'apportion', 'id': 'plan-cap',
            'total': 5, 'unit': 1,
            'periods': [{'period': 'p1', 'cap': 2}, {'period': 'p2', 'cap': 4}],
            'assignments': {
                'a': 'p1', 'b': 'p1', 'c': 'p1',
                'd': 'p2', 'e': 'conflict'},
            'tenant': 't', 'issue_claims': True})
        self.assertEqual(cap_plan['balances']['cap_blocked'], 1)
        self.assertEqual(cap_plan['balances']['unattributed'], 1)
        self.assertEqual(cap_plan['ledgers'], {'p1': 2, 'p2': 1})
        self.assertTrue(cap_plan['conserved'])

        # total 5 settled in lots of 2 leaves a one-unit tail below one lot.
        dust_plan = act(self.db, 'credit', {
            'event_id': 'ap-dust', 'op': 'allocate', 'id': 'plan-dust',
            'total': 5, 'unit': 2, 'tenant': 't', 'issue_claims': True,
            'periods': [{'period': 'p1', 'cap': 10}, {'period': 'p2', 'cap': 10}],
            'uids': [{'uid': 'a', 'period': 'p1'}, {'uid': 'b', 'period': 'p2'}]})
        self.assertEqual(dust_plan['balances']['dust'], 1)
        self.assertEqual(dust_plan['ledger_total'], 4)
        self.assertTrue(dust_plan['conserved'])

    def test_downgrade_adjusts_unsigned_then_reversed_but_not_signed(self):
        self.ready_cases()
        self.approve(id='plan-d', event_id='approve-d', tenant='tenant-d', issue_claims=True)
        for period, receipt in (('p1', 'rcpt-d1'), ('p2', 'rcpt-d2')):
            self.assertEqual(act(self.db, 'credit', {
                'event_id': f'set-{period}', 'op': 'settle', 'id': receipt,
                'claim': f'plan-d:{period}', 'amount': 2, 'actor': 'billing'})['decision'], 'settled')

        # Removing the still-unattributed conflict UID spends pending balance.
        down = act(self.db, 'credit', {
            'event_id': 'dg1', 'op': 'downgrade', 'id': 'dg1', 'plan': 'plan-d',
            'remove_uids': ['uid-5'], 'reason': 'review'})
        self.assertEqual(down['decision'], 'downgrade_applied')
        self.assertEqual(down['reduced'], 1)
        self.assertEqual(one(self.db, "SELECT total FROM apportion_plans WHERE id='plan-d'")['total'], 4)
        self.assertTrue(down['conserved'])

        # Everything left is signed and unreversed: further reduction is locked.
        locked = act(self.db, 'credit', {
            'event_id': 'dg2', 'op': 'reassess', 'id': 'dg2', 'plan': 'plan-d', 'reduce': 1})
        self.assertEqual(locked['decision'], 'downgrade_locked_by_signed')
        self.assertEqual(locked['signed_locked'], 4)
        self.assertEqual(one(self.db, "SELECT total FROM apportion_plans WHERE id='plan-d'")['total'], 4)

        # Explicit reversal opens that balance to downgrade; original receipt kept.
        rev = act(self.db, 'credit', {
            'event_id': 'rev-d2', 'op': 'reverse', 'id': 'rev-d2',
            'receipt': 'rcpt-d2', 'reason': 'partial-downgrade'})
        self.assertEqual(rev['decision'], 'reversed')
        down2 = act(self.db, 'credit', {
            'event_id': 'dg3', 'op': 'review_adjust', 'id': 'dg3', 'plan': 'plan-d',
            'reduce': 1, 'reason': 'write-off reversed'})
        self.assertEqual(down2['decision'], 'downgrade_applied')
        self.assertEqual(down2['total'], 3)
        self.assertTrue(down2['conserved'])
        self.assertEqual(one(self.db, "SELECT status FROM settlement_receipts WHERE id='rcpt-d1'")['status'], 'signed')
        self.assertEqual(one(self.db, "SELECT status FROM settlement_receipts WHERE id='rcpt-d2'")['status'], 'reversed')

        verified = replay(str(self.path))
        self.assertTrue(verified['verified'], verified)

    def test_downgrade_reduces_unsigned_period_share(self):
        plan = act(self.db, 'credit', {
            'event_id': 'ap-u', 'op': 'apportion', 'id': 'plan-u',
            'total': 4, 'unit': 1, 'tenant': 't', 'issue_claims': True,
            'periods': [{'period': 'p1', 'cap': 2}, {'period': 'p2', 'cap': 4}],
            'assignments': {'u1': 'p1', 'u2': 'p1', 'u3': 'p2', 'u4': 'p2'}})
        self.assertTrue(plan['conserved'])
        down = act(self.db, 'credit', {
            'event_id': 'dgu', 'op': 'downgrade', 'id': 'dgu', 'plan': 'plan-u',
            'remove_uids': ['u1']})
        self.assertEqual(down['decision'], 'downgrade_applied')
        claim = one(self.db, "SELECT amount FROM credit_claims WHERE id='plan-u:p1'")
        self.assertEqual(claim['amount'], 1)
        status = act(self.db, 'credit', {'event_id': 'bal-u', 'op': 'balances', 'plan': 'plan-u'})
        self.assertEqual(status['total'], 3)
        self.assertEqual(status['ledgers'], {'p1': 1, 'p2': 2})
        self.assertTrue(status['conserved'])

    def test_late_proof_into_saturated_signed_period_is_named_cap_balance(self):
        self.ready_cases()
        self.approve(id='plan-s', event_id='approve-s', tenant='tenant-s', issue_claims=True)
        act(self.db, 'credit', {'event_id': 'set-s1', 'op': 'settle', 'id': 'rcpt-s1',
                                'claim': 'plan-s:p1', 'amount': 2, 'actor': 'billing'})
        act(self.db, 'credit', {'event_id': 'set-s2', 'op': 'settle', 'id': 'rcpt-s2',
                                'claim': 'plan-s:p2', 'amount': 2, 'actor': 'billing'})
        # Proof assigns uid-5 to p1 whose cap 2 is already fully signed.
        proof = act(self.db, 'credit', {
            'event_id': 'proof-s', 'op': 'confirm_attribution', 'id': 'proof-s',
            'plan': 'plan-s', 'uid': 'uid-5', 'period': 'p1', 'actor': 'audit'})
        self.assertEqual(proof['decision'], 'attribution_confirmed')
        self.assertEqual(proof['booked'], 0)
        self.assertEqual(proof['cap_blocked'], 1)
        self.assertEqual(proof['routing'], 'cap_blocked')
        status = act(self.db, 'credit', {'event_id': 'bal-s', 'op': 'balances', 'plan': 'plan-s'})
        self.assertEqual(status['balances']['cap_blocked'], 1)
        self.assertEqual(status['ledgers'], {'p1': 2, 'p2': 2})
        self.assertEqual(status['unallocated'], 1)
        self.assertTrue(status['conserved'])

    def test_downgrade_lock_is_contract_independent(self):
        self.ready_cases()
        self.approve(id='plan-r', event_id='approve-r', tenant='tenant-r',
                     contract='return_period', issue_claims=True)
        for p in ('p1', 'p2'):
            act(self.db, 'credit', {'event_id': f'set-r-{p}', 'op': 'settle', 'id': f'rcpt-r-{p}',
                                    'claim': f'plan-r:{p}', 'amount': 2, 'actor': 'billing'})
        locked = act(self.db, 'credit', {
            'event_id': 'dg-r', 'op': 'downgrade', 'id': 'dg-r', 'plan': 'plan-r', 'reduce': 2})
        self.assertEqual(locked['decision'], 'downgrade_locked_by_signed')
        self.assertEqual(locked['signed_locked'], 4)

    def test_split_period_keeps_legacy_shape_and_accepts_total_plan(self):
        self.ready_cases()
        # Legacy read-only behaviour is unchanged without a total constraint.
        act(self.db, 'credit', {
            'event_id': 'legacy-claim', 'op': 'claim', 'id': 'lc1',
            'case': 'case-ap1', 'tenant': 't', 'period': 'p1', 'amount': 1, 'cap': 2})
        legacy = act(self.db, 'credit', {'event_id': 'legacy-split', 'op': 'split_period', 'case': 'case-ap1'})
        # Only one period exists on this case so the old guard answers.
        self.assertEqual(legacy, {'decision': 'not_cross_period'})

        approved = act(self.db, 'credit', {
            'event_id': 'split-approve', 'op': 'split_period',
            'id': 'plan-sp', 'total': 5,
            'periods': [{'period': 'p1', 'cap': 2, 'case': 'case-ap1'},
                        {'period': 'p2', 'cap': 4, 'case': 'case-ap2'}]})
        self.assertEqual(approved['decision'], 'split_confirmed')
        self.assertEqual(approved['periods'], ['p1', 'p2'])
        self.assertIn('allocation', approved)
        self.assertTrue(approved['conserved'])


if __name__ == '__main__':
    unittest.main()

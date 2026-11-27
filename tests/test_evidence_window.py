import json
import tempfile
import unittest
from pathlib import Path

from sloattrib.store import act, connect, one, many, replay

ROOT = Path(__file__).resolve().parents[1]


class EvidenceWindow(unittest.TestCase):
    """Sealed-window adjudication driven by evidence segments rather than the
    decision clock, with bounded failure counts, versioned cases and per-UID
    claim reservations."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'evidence.sqlite'
        self.db = connect(str(self.path))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def run_fixture(self, name):
        subsystem = name.rsplit('_', 1)[1]
        steps = json.loads((ROOT / 'fixtures' / f'{name}.json').read_text(encoding='utf-8'))
        return [act(self.db, subsystem, step) for step in steps]

    def base(self):
        self.run_fixture('evidence_window_slo')
        return self.run_fixture('evidence_window_cause')

    def case(self, case_id):
        return one(self.db, 'SELECT * FROM cause_cases WHERE id=?', (case_id,))

    def test_decision_clock_inside_waiver_no_longer_zeros_failures(self):
        """The exact reported defect: decide at t=7 while a waiver is active
        must not erase failures whose evidence sits outside [5,10)."""
        self.base()
        case = self.case('edge-case-v1')
        # u1 and u3 are wholly before the waiver: two certain failures.
        # u2 spans [4,7), only partially covered, so it stays possible.
        self.assertEqual(case['status'], 'decided')
        self.assertEqual(case['failed_requests'], 2)
        self.assertEqual(case['failures_lower'], 2)
        self.assertEqual(case['failures_upper'], 3)
        self.assertEqual(case['allowed_failures'], 1)
        self.assertEqual(case['version'], 1)
        # The two sources for u1 contribute a single failure.
        rows = many(self.db, "SELECT request_uid FROM failure_samples")
        self.assertEqual(len(rows), 5)

    def test_duplicate_non_client_evidence_counts_once_per_uid(self):
        self.base()
        # Add another trusted observation for an already-failing UID.
        result = act(self.db, 'cause', {
            'event_id': 'ew-u1-c', 'op': 'sample', 'id': 'ew-u1-c',
            'window': 'edge-window', 'request_uid': 'u1',
            'source': 'prober', 'at': 1, 'kind': 'dependency'})
        self.assertEqual(result['decision'], 'sample_recorded')
        result = act(self.db, 'cause', {
            'event_id': 'ew-redo-1', 'op': 'decide', 'id': 'edge-case-v1b',
            'window': 'edge-window', 'topology': 'topo-edge', 'at': 8})
        self.assertEqual(result['failures_lower'], 2)
        self.assertEqual(result['failures_upper'], 3)
        # The new dependency evidence for u1 shifts attribution.
        self.assertEqual(result['liable'], 'dependency')

    def test_claim_draft_then_corrective_endorsement_re_decides_and_freezes_only_unsupported(self):
        self.base()
        # Provisional claim over the provable UIDs, one line each.
        claim = {
            'event_id': 'claim-lines', 'op': 'claim', 'id': 'claim-prov',
            'case': 'edge-case-v1', 'tenant': 'tenant-a', 'period': 'p1',
            'amount': 60, 'cap': 100,
            'lines': [{'request_uid': 'u1', 'amount': 30},
                      {'request_uid': 'u3', 'amount': 30}]}
        self.assertEqual(act(self.db, 'credit', claim)['decision'], 'claim_reserved')
        # A reservation cannot cite a UID that is only possibly failing.
        denied = dict(claim, event_id='claim-pending', id='claim-pending',
                      tenant='tenant-b',
                      lines=[{'request_uid': 'u2', 'amount': 10}], amount=10)
        self.assertEqual(act(self.db, 'credit', denied)['decision'], 'claim_denied')

        # Late dual-signed waivers then cover the two certain-failure UIDs,
        # while an endorsement narrows u2 inside the original waiver.
        for event_id, start, end in (('waiver-a', 1, 3), ('waiver-b', 2, 3)):
            self.assertEqual(act(self.db, 'slo', {
                'event_id': event_id, 'op': 'waiver', 'id': event_id,
                'objective': 'edge-api', 'start': start, 'end': end,
                'security_actor': 'sec', 'owner_actor': 'owner'})['decision'],
                'waiver_active')
        reconciliation = act(self.db, 'cause', {
            'event_id': 'recon-u2', 'op': 'reconcile', 'id': 'recon-u2',
            'window': 'edge-window', 'request_uid': 'u2', 'source': 'monitor',
            'start': 5, 'end': 6})
        self.assertEqual(reconciliation['decision'], 'reconciliation_recorded')

        result = act(self.db, 'cause', {
            'event_id': 'ew-redo-2', 'op': 'decide', 'id': 'edge-case-v2',
            'window': 'edge-window', 'topology': 'topo-edge', 'at': 20})
        # Every UID is now fully inside dual-signed coverage: close the case.
        self.assertEqual(result['decision'], 'within_target')
        self.assertEqual(result['version'], 2)
        self.assertEqual(result['failures_lower'], 0)
        self.assertEqual(result['failures_upper'], 0)

        # The sealed prior version is not rewritten in place.
        old = self.case('edge-case-v1')
        self.assertEqual(old['status'], 'sealed')
        self.assertEqual(old['failed_requests'], 2)

        # Both reserved lines lost business support, so the draft is frozen,
        # each line accounted individually.
        self.assertEqual(one(self.db,
            "SELECT status FROM credit_claims WHERE id='claim-prov'")['status'], 'frozen')
        line_status = {row['request_uid']: row['status'] for row in many(
            self.db, "SELECT request_uid,status FROM credit_claim_lines WHERE claim_id='claim-prov'")}
        self.assertEqual(line_status, {'u1': 'frozen', 'u3': 'frozen'})
        self.assertEqual(act(self.db, 'credit', {
            'event_id': 'settle-frozen', 'op': 'settle', 'id': 'receipt-frozen',
            'claim': 'claim-prov', 'amount': 60, 'actor': 'billing'})['decision'],
            'settlement_denied')

    def test_independent_uid_keeps_reservation_when_only_another_loses_support(self):
        self.base()
        act(self.db, 'credit', {
            'event_id': 'claim-two', 'op': 'claim', 'id': 'claim-two',
            'case': 'edge-case-v1', 'tenant': 'tenant-a', 'period': 'p1',
            'amount': 60, 'cap': 100,
            'lines': [{'request_uid': 'u1', 'amount': 30},
                      {'request_uid': 'u3', 'amount': 30}]})
        # Only u1 gains full waiver coverage; u3 stays provable.  An
        # endorsement first narrows u1's segment so the new waiver can cover
        # it without also swallowing u3's observation at t=2.
        self.assertEqual(act(self.db, 'cause', {
            'event_id': 'recon-u1', 'op': 'reconcile', 'id': 'recon-u1',
            'window': 'edge-window', 'request_uid': 'u1', 'source': 'monitor',
            'start': 1, 'end': 2})['decision'], 'reconciliation_recorded')
        act(self.db, 'slo', {
            'event_id': 'waiver-u1', 'op': 'waiver', 'id': 'waiver-u1',
            'objective': 'edge-api', 'start': 1, 'end': 2,
            'security_actor': 'sec', 'owner_actor': 'owner'})
        act(self.db, 'cause', {
            'event_id': 'ew-redo-3', 'op': 'decide', 'id': 'edge-case-v3',
            'window': 'edge-window', 'topology': 'topo-edge', 'at': 20})
        lines = {row['request_uid']: row['status'] for row in many(
            self.db, "SELECT request_uid,status FROM credit_claim_lines WHERE claim_id='claim-two'")}
        self.assertEqual(lines, {'u1': 'frozen', 'u3': 'supported'})
        # The claim remains a draft because an independent UID still supports it.
        self.assertEqual(one(self.db,
            "SELECT status FROM credit_claims WHERE id='claim-two'")['status'], 'draft')

    def test_bounds_straddling_budget_waits_for_evidence_and_blocks_claims(self):
        """Allowed == 1, one certain failure plus one possible failure: the
        bounds straddle the budget, so the case waits and no credit is held."""
        self.base()
        # A fresh certain failure far from any waiver coverage.
        act(self.db, 'cause', {
            'event_id': 'ew-u5', 'op': 'sample', 'id': 'ew-u5',
            'window': 'edge-window', 'request_uid': 'u5',
            'source': 'monitor', 'at': 40, 'kind': 'service'})
        # Waive u3 completely; u1 is waived along with it (same timestamps),
        # u2 stays only partly covered (possible), u5 stays certain.
        act(self.db, 'slo', {
            'event_id': 'waiver-u3', 'op': 'waiver', 'id': 'waiver-u3',
            'objective': 'edge-api', 'start': 1, 'end': 3,
            'security_actor': 'sec', 'owner_actor': 'owner'})
        result = act(self.db, 'cause', {
            'event_id': 'ew-wait', 'op': 'decide', 'id': 'edge-case-wait',
            'window': 'edge-window', 'topology': 'topo-edge', 'at': 9})
        self.assertEqual(result['decision'], 'awaiting_evidence')
        self.assertEqual(result['failures_lower'], 1)
        self.assertEqual(result['failures_upper'], 2)
        self.assertEqual(act(self.db, 'credit', {
            'event_id': 'no-hold', 'op': 'claim', 'id': 'claim-blocked',
            'case': 'edge-case-wait', 'tenant': 't', 'period': 'p1',
            'amount': 5, 'cap': 100})['decision'], 'claim_denied')

        # Endorsement narrows u2 into the waiver: upper bound drops to the
        # budget, so the waiting case can now be closed on fresh evidence.
        act(self.db, 'cause', {
            'event_id': 'recon-wait', 'op': 'reconcile', 'id': 'recon-wait',
            'window': 'edge-window', 'request_uid': 'u2', 'source': 'monitor',
            'start': 5, 'end': 6})
        result = act(self.db, 'cause', {
            'event_id': 'ew-close', 'op': 'decide', 'id': 'edge-case-close',
            'window': 'edge-window', 'topology': 'topo-edge', 'at': 30})
        self.assertEqual(result['decision'], 'within_target')
        self.assertEqual(result['failures_lower'], 1)
        self.assertEqual(result['failures_upper'], 1)
        # The waiting version is superseded, never mutated.
        self.assertEqual(self.case('edge-case-wait')['status'], 'superseded')

    def test_upper_bound_within_budget_closes_case_without_waiting(self):
        self.base()
        # One certain failure outside every waiver interval.
        act(self.db, 'cause', {
            'event_id': 'ew-u5-close', 'op': 'sample', 'id': 'ew-u5-close',
            'window': 'edge-window', 'request_uid': 'u5',
            'source': 'monitor', 'at': 40, 'kind': 'service'})
        # Cover u1 and u3 entirely, and narrow u2 into coverage; only u5
        # remains failing, exactly at the budget of one.
        act(self.db, 'slo', {
            'event_id': 'waiver-close', 'op': 'waiver', 'id': 'waiver-close',
            'objective': 'edge-api', 'start': 1, 'end': 3,
            'security_actor': 'sec', 'owner_actor': 'owner'})
        act(self.db, 'cause', {
            'event_id': 'recon-close', 'op': 'reconcile', 'id': 'recon-close',
            'window': 'edge-window', 'request_uid': 'u2', 'source': 'monitor',
            'start': 5, 'end': 6})
        result = act(self.db, 'cause', {
            'event_id': 'ew-ok', 'op': 'decide', 'id': 'edge-case-ok',
            'window': 'edge-window', 'topology': 'topo-edge', 'at': 12})
        self.assertEqual(result['decision'], 'within_target')
        self.assertEqual(result['failures_lower'], 1)
        self.assertEqual(result['failures_upper'], 1)

    def test_endorsement_must_match_a_trusted_source_and_observed_range(self):
        self.base()
        # Unknown source.
        self.assertEqual(act(self.db, 'cause', {
            'event_id': 'recon-bad-source', 'op': 'reconcile', 'id': 'recon-bad-source',
            'window': 'edge-window', 'request_uid': 'u2', 'source': 'outsider',
            'start': 5, 'end': 6})['decision'], 'reconciliation_denied')
        # Range outside the observed [4,7) bounds.
        self.assertEqual(act(self.db, 'cause', {
            'event_id': 'recon-bad-range', 'op': 'reconcile', 'id': 'recon-bad-range',
            'window': 'edge-window', 'request_uid': 'u2', 'source': 'monitor',
            'start': 5, 'end': 8})['decision'], 'reconciliation_denied')

    def test_named_source_endorsements_intersect_and_conflict_is_rejected(self):
        self.base()
        # Two trusted sources both observed u2; overlapping endorsements
        # intersect to the confirmed sampling range.
        self.assertEqual(act(self.db, 'cause', {
            'event_id': 'recon-u2-mon', 'op': 'reconcile', 'id': 'recon-u2-mon',
            'window': 'edge-window', 'request_uid': 'u2', 'source': 'monitor',
            'start': 4, 'end': 7})['decision'], 'reconciliation_recorded')
        self.assertEqual(act(self.db, 'cause', {
            'event_id': 'recon-u2-gw', 'op': 'reconcile', 'id': 'recon-u2-gw',
            'window': 'edge-window', 'request_uid': 'u2', 'source': 'gateway',
            'start': 6, 'end': 7})['decision'], 'reconciliation_recorded')
        # Intersection [6,7) is wholly inside the [5,10) waiver, so u2 drops
        # out of the possible-failure set.
        result = act(self.db, 'cause', {
            'event_id': 'ew-narrow', 'op': 'decide', 'id': 'edge-case-narrow',
            'window': 'edge-window', 'topology': 'topo-edge', 'at': 11})
        self.assertEqual(result['failures_upper'], 2)
        # A third range disjoint from the gateway endorsement is rejected.
        self.assertEqual(act(self.db, 'cause', {
            'event_id': 'recon-u2-bad', 'op': 'reconcile', 'id': 'recon-u2-bad',
            'window': 'edge-window', 'request_uid': 'u2', 'source': 'monitor',
            'start': 4, 'end': 5})['decision'], 'endorsement_conflict')

    def test_sealed_case_can_be_confirmed_manually_and_replay_is_exact(self):
        self.base()
        sealed = act(self.db, 'cause', {
            'event_id': 'seal-v1', 'op': 'seal_case', 'case': 'edge-case-v1'})
        self.assertEqual(sealed['decision'], 'case_sealed')
        # A sealed version cannot be claimed against.
        self.assertEqual(act(self.db, 'credit', {
            'event_id': 'claim-sealed', 'op': 'claim', 'id': 'claim-sealed',
            'case': 'edge-case-v1', 'tenant': 't', 'period': 'p1',
            'amount': 5, 'cap': 100})['decision'], 'claim_denied')
        result = replay(str(self.path))
        self.assertTrue(result, result)
        self.assertTrue(result['verified'], result)


if __name__ == '__main__':
    unittest.main()

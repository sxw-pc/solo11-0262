import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from sloattrib.custody import digest, record_digest, version_coords
from sloattrib.store import act, connect, many, one, replay

ROOT = Path(__file__).resolve().parents[1]


class CustodyScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hot_path = str(Path(self.tmp.name) / 'hot.sqlite')
        self.cold_path = str(Path(self.tmp.name) / 'cold.sqlite')
        self.hot = connect(self.hot_path)
        for row in json.loads((ROOT / 'fixtures' / 'custody_shared.json').read_text(encoding='utf-8')):
            subsystem = row.pop('subsystem')
            act(self.hot, subsystem, row)
            row['subsystem'] = subsystem
        self.cold = connect(self.cold_path)

    def tearDown(self):
        self.hot.close()
        self.cold.close()
        self.tmp.cleanup()

    def _load_fixture_rows(self):
        return json.loads((ROOT / 'fixtures' / 'custody_shared.json').read_text(encoding='utf-8'))

    def _seal_projected(self, package_id, case_id, refs, grants, project=None, event_id=None):
        command = {'event_id': event_id or f'seal-{package_id}', 'op': 'seal',
                   'id': package_id, 'case': case_id, 'contract': 'projected',
                   'refs': refs, 'grants': grants}
        if project is not None:
            command['project'] = project
        return act(self.hot, 'custody', command)

    def _accept(self, db, receipt_id, sealed, event_id=None):
        return act(db, 'custody', {'event_id': event_id or f'accept-{receipt_id}',
                                   'op': 'accept', 'id': receipt_id,
                                   'digest': sealed['digest'], 'bundle': sealed['bundle']})

    def test_projected_package_keeps_only_scoped_actions_and_rebuilds_disputes(self):
        sealed = self._seal_projected(
            'proj-case-1', 'case-1',
            refs=['c2'], project=['s1', 's2', 's3', 's4', 'c1'],
            grants=[{'id': 'grant-c2', 'event_id': 'c2'}])
        self.assertEqual(sealed['decision'], 'custody_sealed')
        bundle = sealed['bundle']
        # The package actually leaves the unrelated second case and prefix
        # originals behind, while keeping case-1's own deciding actions.
        included = [r['payload']['event_id'] for r in bundle['events']]
        self.assertEqual(included, ['c3', 'c4', 'c6', 'c7'])
        self.assertNotIn('c5', included)
        self.assertNotIn('c8', included)
        # Prefix originals survive only as projections plus version bindings.
        projected_ids = {b['event_id'] for b in bundle['bindings']} - set(included)
        self.assertIn('c2', projected_ids)
        self.assertIn('s1', projected_ids)
        self.assertIn('c1', projected_ids)
        self.assertIn('c2', sealed['pinned'])

        result = self._accept(self.cold, 'receipt-1', sealed)
        self.assertEqual(result['decision'], 'custody_accepted')
        self.assertEqual(result['case_status'], 'decided')
        self.assertEqual(result['contract'], 'projected')
        # The dispute scope of case-1 at the time is rebuilt, including its
        # resolution decision and the evidence that opened the dispute.
        self.assertEqual([d['id'] for d in result['disputes']], ['dispute-1'])
        self.assertEqual(result['disputes'][0]['status'], 'resolved')
        self.assertEqual(result['disputes'][0]['evidence'], 'independent-counter')
        review = act(self.cold, 'custody', {'event_id': 'review-1', 'op': 'review',
                                            'package': 'proj-case-1'})
        self.assertEqual(review['decision'], 'custody_review_ok')
        self.assertEqual(review['requires_retrieval'], ['c2'])
        self.assertTrue(replay(self.cold_path)['verified'])

    def test_projection_cannot_demote_events_outside_case_scope(self):
        sealed = self._seal_projected(
            'bad-ref', 'case-1', refs=['c5'], grants=[], project=[])
        self.assertEqual(sealed['decision'], 'custody_scope_invalid')
        self.assertEqual(sealed['detail'], 'ref_outside_scope')

    def test_raw_table_dump_and_ungrounded_grant_cannot_pretend_to_be_custody(self):
        # A grant may only cover an original actually summarised via refs.
        bad_grant = self._seal_projected(
            'bad-grant', 'case-1', refs=['c2'],
            project=['s1', 's2', 's3', 's4', 'c1'],
            grants=[{'id': 'grant-stray', 'event_id': 'c3'}])
        self.assertEqual(bad_grant['decision'], 'custody_scope_invalid')
        self.assertEqual(bad_grant['detail'], 'grant_invalid')
        # Copying only the current window/topology/case tables without an
        # event chain or version bindings cannot prove the review scope.
        raw_dump = {'case': 'case-1', 'contract': 'projected',
                    'package_id': 'raw', 'anchor_seq': 0,
                    'anchor': '0' * 64, 'events': [], 'projections': [],
                    'bindings': [], 'retrieval': [],
                    'manifest': digest({'anchor': '0' * 64, 'bindings': [],
                                        'projections': []})}
        result = act(self.cold, 'custody',
                     {'event_id': 'raw-accept', 'op': 'accept', 'id': 'raw-receipt',
                      'digest': digest(raw_dump), 'bundle': raw_dump})
        self.assertEqual(result['decision'], 'custody_unverifiable')
        # An unknown contract name is never silently accepted.
        weird = {'case': 'case-1', 'contract': 'snapshot', 'events': []}
        weird_result = act(self.cold, 'custody',
                           {'event_id': 'weird-accept', 'op': 'accept', 'id': 'weird-receipt',
                            'digest': digest(weird), 'bundle': weird})
        self.assertEqual(weird_result['decision'], 'custody_unverifiable')

    def test_same_uid_cross_target_version_replacement_is_rejected(self):
        sealed = self._seal_projected(
            'proj-swap', 'case-1',
            refs=['c2'], project=['s1', 's2', 's3', 's4', 'c1'],
            grants=[{'id': 'grant-swap', 'event_id': 'c2'}])
        bundle = sealed['bundle']
        replacement = next(r for r in self._load_fixture_rows() if r['event_id'] == 'c2')
        replacement = {k: v for k, v in replacement.items() if k != 'subsystem'}
        # Same request UID and source, but pointing at a different window:
        # a cross-target-version original wearing the same identity.
        replacement['window'] = 'window-other'
        swapped_record = {'subsystem': 'cause', 'payload': replacement,
                          'result': {'decision': 'sample_recorded'}}
        tampered = json.loads(json.dumps(bundle))
        # Re-seal the whole envelope coherently, binding and grant included,
        # so the only remaining defence is the incident-time version check.
        tampered['bindings'] = [
            dict(b, digest=record_digest(swapped_record),
                 version=version_coords(swapped_record)) if b['event_id'] == 'c2' else b
            for b in tampered['bindings']]
        tampered['retrieval'][0]['digest'] = record_digest(swapped_record)
        tampered['retrieval'][0]['version'] = version_coords(swapped_record)
        tampered['manifest'] = digest({'anchor': tampered['anchor'],
                                       'bindings': tampered['bindings'],
                                       'projections': tampered['projections']})
        result = act(self.cold, 'custody',
                     {'event_id': 'swap-accept', 'op': 'accept', 'id': 'swap-receipt',
                      'digest': digest(tampered), 'bundle': tampered})
        self.assertEqual(result['decision'], 'custody_evidence_version_conflict')

        # Mutating an included action's body while keeping its UID must fail
        # the digest/version check as well, not silently replay as another case.
        direct = json.loads(json.dumps(sealed['bundle']))
        direct['events'][0]['payload']['request_uid'] = 'req-foreign'
        direct_accept = {'event_id': 'direct-accept', 'op': 'accept', 'id': 'direct-receipt',
                         'digest': digest(direct), 'bundle': direct}
        self.assertEqual(act(self.cold, 'custody', direct_accept)['decision'],
                         'custody_evidence_version_conflict')

    def test_shared_original_is_retained_and_second_case_cannot_steal_takeover(self):
        first = self._seal_projected(
            'proj-a', 'case-1',
            refs=['c2'], project=['s1', 's2', 's3', 's4', 'c1'],
            grants=[{'id': 'grant-shared', 'event_id': 'c2'}])
        self.assertEqual(self._accept(self.cold, 'receipt-a', first)['decision'],
                         'custody_accepted')
        # Cold storage stops answering; the hot store keeps taking new case
        # citations on the same shared original (c2/c3 under window-1).
        second = self._seal_projected(
            'proj-b', 'case-2',
            refs=['c2'], project=['s1', 's2', 's3', 's4', 'c1'],
            grants=[{'id': 'grant-shared', 'event_id': 'c2'}],
            event_id='seal-proj-b')
        self.assertEqual(second['decision'], 'custody_sealed')
        result = self._accept(self.cold, 'receipt-b', second, event_id='accept-receipt-b')
        # Being created later never grants case-2 case-1's retrieval right.
        self.assertEqual(result['decision'], 'custody_grant_scope_conflict')
        self.assertEqual(result['grant'], 'grant-shared')
        # Each takeover credential covers exactly one review scope: once
        # case-2 presents its own distinct grant for the same original, it is
        # accepted independently without inheriting case-1's credential.
        own = self._seal_projected(
            'proj-b-own', 'case-2',
            refs=['c2'], project=['s1', 's2', 's3', 's4', 'c1'],
            grants=[{'id': 'grant-case-2', 'event_id': 'c2'}],
            event_id='seal-proj-b-own')
        own_result = self._accept(self.cold, 'receipt-b-own', own,
                                  event_id='accept-receipt-b-own')
        self.assertEqual(own_result['decision'], 'custody_accepted')
        owners = many(self.cold, 'SELECT id, case_id FROM custody_grants ORDER BY id')
        self.assertEqual(owners, [
            {'id': 'grant-case-2', 'case_id': 'case-2'},
            {'id': 'grant-shared', 'case_id': 'case-1'},
        ])

        sweep = act(self.hot, 'custody', {'event_id': 'sweep', 'op': 'sweep'})
        # The shared originals are still cited by live cases and pinned by
        # sealed packages: nothing owned becomes orphaned.
        for event_id in ('c2', 'c3', 'c4', 'c5', 'c6', 'c7'):
            self.assertIn(event_id, sweep['retained'])
        # The duplicate-sample action was rejected (no business row cites it)
        # and no package pinned it, so it is the only releasable original.
        self.assertEqual(sweep['releasable'], ['dup-1'])
        self.assertEqual(sweep['decision'], 'custody_sweep')

    def test_full_contract_opens_case_offline_while_projected_needs_retrieval(self):
        full = act(self.hot, 'custody', {'event_id': 'fr-seal-full', 'op': 'seal',
                                         'id': 'full-case-1', 'case': 'case-1'})
        self.assertEqual(full['contract'], 'full')
        full_result = self._accept(self.cold, 'full-receipt', full,
                                   event_id='fr-accept-full')
        self.assertEqual(full_result['decision'], 'custody_accepted')
        # Hot store unavailable: the full-text reviewer reads the explicit
        # contemporaneous copy on its own.
        offline = act(self.cold, 'custody', {'event_id': 'fr-cmd-offline-read', 'op': 'retrieve',
                                             'package': 'full-case-1', 'evidence_id': 'c2',
                                             'hot_available': False})
        self.assertEqual(offline['decision'], 'custody_evidence_served')
        self.assertEqual(offline['payload']['request_uid'], 'req-a')

        projected = self._seal_projected(
            'proj-retrieve', 'case-1',
            refs=['c2'], project=['s1', 's2', 's3', 's4', 'c1'],
            grants=[{'id': 'grant-c2-only', 'event_id': 'c2'}])
        self.assertEqual(self._accept(self.cold, 'proj-receipt', projected,
                                      event_id='cmd-accept-proj')['decision'],
                         'custody_accepted')
        # No retrieval right on this original: the old summary cannot prove it.
        no_grant = act(self.cold, 'custody', {'event_id': 'fr-cmd-no-grant', 'op': 'retrieve',
                                              'package': 'proj-retrieve', 'evidence_id': 's1',
                                              'hot_available': True})
        self.assertEqual(no_grant['decision'], 'custody_review_restricted')
        # Hot store down: a projected custody holder cannot fabricate the fact.
        down = act(self.cold, 'custody', {'event_id': 'fr-cmd-hot-down', 'op': 'retrieve',
                                          'package': 'proj-retrieve', 'evidence_id': 'c2',
                                          'hot_available': False})
        self.assertEqual(down['decision'], 'custody_review_restricted')
        self.assertEqual(down['reason'], 'original_not_retrievable')
        # With the hot store back, only the incident-time version is served.
        original = next(r for r in self._load_fixture_rows() if r['event_id'] == 'c2')
        original = {k: v for k, v in original.items() if k != 'subsystem'}
        evidence = {'subsystem': 'cause', 'payload': original,
                    'result': {'decision': 'sample_recorded'}}
        served = act(self.cold, 'custody', {'event_id': 'fr-cmd-hot-serve', 'op': 'retrieve',
                                            'package': 'proj-retrieve', 'evidence_id': 'c2',
                                            'hot_available': True, 'evidence': evidence})
        self.assertEqual(served['decision'], 'custody_evidence_served')
        self.assertEqual(served['payload']['window'], 'window-1')
        # A later conflict supersedes the original version: the hot store can
        # only offer a different body now, which must be refused as restricted
        # rather than letting the old summary attest to the new fact.
        stale = dict(evidence, payload=dict(original, window='window-other'))
        conflict = act(self.cold, 'custody', {'event_id': 'fr-cmd-hot-conflict', 'op': 'retrieve',
                                              'package': 'proj-retrieve', 'evidence_id': 'c2',
                                              'hot_available': True, 'evidence': stale})
        self.assertEqual(conflict['decision'], 'custody_evidence_version_conflict')
        # The full contract still reads its explicit contemporaneous copy.
        still = act(self.cold, 'custody', {'event_id': 'fr-cmd-full-still', 'op': 'retrieve',
                                           'package': 'full-case-1', 'evidence_id': 'c2',
                                           'hot_available': False})
        self.assertEqual(still['decision'], 'custody_evidence_served')

    def test_cold_storage_retry_is_idempotent_for_the_same_request(self):
        sealed = self._seal_projected(
            'proj-retry', 'case-1',
            refs=['c2'], project=['s1', 's2', 's3', 's4', 'c1'],
            grants=[{'id': 'grant-retry', 'event_id': 'c2'}])
        first = self._accept(self.cold, 'retry-receipt', sealed)
        again = self._accept(self.cold, 'retry-receipt', sealed, event_id='retry-again')
        self.assertEqual(first['decision'], 'custody_accepted')
        self.assertTrue(again['retry'])
        self.assertEqual(again['digest'], first['digest'])
        self.assertEqual(again['case_status'], first['case_status'])
        self.assertEqual(len(many(self.cold, 'SELECT * FROM custody_receipts WHERE id=?',
                                  ('retry-receipt',))), 1)
        # A different bundle reusing the receipt identity is a real conflict.
        other = json.loads(json.dumps(sealed['bundle']))
        other['case'] = 'case-2'
        clash = act(self.cold, 'custody', {'event_id': 'retry-clash', 'op': 'accept',
                                           'id': 'retry-receipt', 'digest': digest(other),
                                           'bundle': other})
        self.assertEqual(clash['decision'], 'custody_receipt_exists')

    def test_original_full_package_remains_acceptable_and_self_contained(self):
        sealed = act(self.hot, 'custody', {'event_id': 'legacy-seal', 'op': 'seal',
                                           'id': 'legacy', 'case': 'case-1'})
        result = self._accept(self.cold, 'legacy-receipt', sealed)
        self.assertEqual(result['decision'], 'custody_accepted')
        # The whole-history package faithfully carries the later topology
        # revision, so the case's terminal status is the post-incident 'stale'.
        self.assertEqual(result['case_status'], 'stale')
        case = one(self.cold, "SELECT * FROM cause_cases WHERE id='case-1'")
        self.assertIsNone(case)  # acceptance records receipts, not hot rows
        review = act(self.cold, 'custody', {'event_id': 'legacy-review', 'op': 'review',
                                            'package': 'legacy'})
        self.assertEqual(review['decision'], 'custody_review_ok')
        self.assertEqual(review['contract'], 'full')
        self.assertEqual(review['case_status'], 'stale')
        self.assertEqual(review['requires_retrieval'], [])
        # The explicit contemporaneous topology generation is inside the copy.
        self.assertIn(
            ('topo-checkout', 1),
            {(r['payload']['id'], r['payload']['generation'])
             for r in sealed['bundle']['events']
             if r['payload'].get('op') == 'topology'})
        self.assertTrue(replay(self.cold_path)['verified'])

    def test_manifest_tampering_and_unknown_anchor_are_surfaceable(self):
        sealed = self._seal_projected(
            'proj-trust', 'case-1',
            refs=['c2'], project=['s1', 's2', 's3', 's4', 'c1'],
            grants=[{'id': 'grant-trust', 'event_id': 'c2'}])
        tampered = json.loads(json.dumps(sealed['bundle']))
        tampered['manifest'] = '0' * 64
        result = act(self.cold, 'custody', {'event_id': 'bad-manifest', 'op': 'accept',
                                            'id': 'bad-manifest-receipt',
                                            'digest': digest(tampered), 'bundle': tampered})
        self.assertEqual(result['decision'], 'custody_manifest_conflict')
        clean = self._accept(
            connect(':memory:'), 'trust-receipt', sealed, event_id='trust-accept')
        self.assertFalse(clean['anchor_trusted'])
        trust_db = connect(':memory:')
        act(trust_db, 'custody', {'event_id': 'mark-trust', 'op': 'trust',
                                  'anchor': sealed['bundle']['anchor']})
        trusted = self._accept(trust_db, 'trust-receipt-2', sealed,
                               event_id='trust-accept-2')
        self.assertTrue(trusted['anchor_trusted'])
        trust_db.close()


if __name__ == '__main__':
    unittest.main()

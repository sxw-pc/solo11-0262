import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from sloattrib.store import act, connect, one, replay

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures'


def decisions(results):
    return [item['result']['decision'] if 'result' in item else item['decision'] for item in results]


def run_fixture(db, name):
    steps = json.loads((FIXTURES / name).read_text(encoding='utf-8'))
    return [act(db, step['subsystem'], {**step['command'], 'event_id': step['event_id']})
            for step in steps]


class HandoverFixtureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'handover.sqlite')
        self.db = connect(self.path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_fulfill_route_hands_over_elected_proposal(self):
        results = run_fixture(self.db, 'register_handover_fulfill.json')
        by_id = {step['event_id']: result for step, result in zip(
            json.loads((FIXTURES / 'register_handover_fulfill.json').read_text(encoding='utf-8')), results)}
        self.assertEqual(by_id['f-vote-a']['decision'], 'vote_recorded')
        self.assertEqual(by_id['f-handover']['decision'], 'handover_opened')
        self.assertEqual(by_id['f-vote-b']['decision'], 'vote_recorded')
        self.assertEqual(by_id['f-new-blocked']['decision'], 'old_position_unresolved')
        applied = by_id['f-apply-p']
        self.assertEqual(applied['decision'], 'written')
        self.assertEqual(applied['value'], '2')
        self.assertEqual(applied['receipt'], 'receipt:slot7:P')
        # Re-issued receipt for a replayed apply, never a second write.
        self.assertEqual(by_id['f-apply-p-receipt-replay']['decision'], 'slot_already_applied')
        self.assertEqual(by_id['f-apply-p-receipt-replay']['receipt'], applied['receipt'])
        self.assertEqual(by_id['f-complete']['decision'], 'new_group_initialized')
        self.assertEqual(by_id['f-new-still-blocked']['decision'], 'position_already_settled')
        readable = by_id['f-read']
        self.assertEqual(readable['decision'], 'position_state')
        self.assertTrue(readable['readable'])
        self.assertEqual((readable['proposal'], readable['value'], readable['epoch']), ('P', '2', 'old'))
        # Handover commands are re-entrant after interruptions.
        self.assertEqual(by_id['f-handover-again']['decision'], 'handover_held')
        self.assertEqual(by_id['f-complete-again']['decision'], 'handover_held')
        self.assertEqual(one(self.db, 'SELECT COUNT(*) AS n FROM register_writes')['n'], 1)
        self.assertEqual(replay(self.path)['verified'], True)

    def test_withdraw_route_publishes_new_group_value_with_verifiable_withdrawal(self):
        results = run_fixture(self.db, 'register_handover_withdraw.json')
        steps = json.loads((FIXTURES / 'register_handover_withdraw.json').read_text(encoding='utf-8'))
        by_id = {step['event_id']: result for step, result in zip(steps, results)}
        self.assertEqual(by_id['w-vote-denied']['decision'], 'voting_closed')
        self.assertEqual(by_id['w-seal']['decision'], 'voting_already_closed')
        self.assertEqual(by_id['w-seal-again']['decision'], 'voting_already_closed')
        self.assertEqual(by_id['w-confirm-b-again']['decision'], 'no_vote_already_confirmed')
        early = by_id['w-withdraw-early']
        self.assertEqual(early['decision'], 'outcome_unverified')
        self.assertEqual(early['unaccounted'], ['C'])
        withdrawn = by_id['w-withdraw-p']
        self.assertEqual(withdrawn['decision'], 'proposal_withdrawn')
        self.assertEqual(withdrawn['receipt'], 'receipt:slot7:P')
        state = withdrawn['voter_state']
        self.assertEqual(state['A']['state'], 'voted')
        self.assertEqual(state['B']['state'], 'no_vote_confirmed')
        self.assertEqual(state['C']['state'], 'no_vote_confirmed')
        self.assertEqual(by_id['w-p-status']['status'], 'withdrawn')
        self.assertEqual(by_id['w-complete']['decision'], 'new_group_initialized')
        q = by_id['w-apply-q']
        self.assertEqual(q['decision'], 'written')
        self.assertEqual((q['proposal'], q['value'], q['epoch']), ('Q', '9', 'new'))
        readable = by_id['w-read']
        self.assertEqual((readable['proposal'], readable['value'], readable['epoch']), ('Q', '9', 'new'))
        self.assertEqual(replay(self.path)['verified'], True)

    def test_late_vote_after_partial_confirmation_can_still_settle_old_proposal(self):
        results = run_fixture(self.db, 'register_handover_pending.json')
        steps = json.loads((FIXTURES / 'register_handover_pending.json').read_text(encoding='utf-8'))
        by_id = {step['event_id']: result for step, result in zip(steps, results)}
        unverified = by_id['p-withdraw-unverified']
        self.assertEqual(unverified['decision'], 'outcome_unverified')
        self.assertEqual(unverified['unaccounted'], ['C'])
        self.assertEqual(by_id['p-new-apply-blocked']['decision'], 'old_position_unresolved')
        # Re-running the ordinary vote command is not a late ballot.
        self.assertEqual(by_id['p-rerun-b-vote']['decision'], 'voting_closed')
        # A confirmed no-vote node can never turn in a late ballot.
        self.assertEqual(by_id['p-b-late-confirmed']['decision'], 'node_confirmed_no_vote')
        self.assertEqual(by_id['p-c-late-no-proof']['decision'], 'late_vote_needs_sealed_proof')
        # C left the old roster and joined the new group, but its frozen old
        # vote power is untouched and settles the old position.
        self.assertEqual(by_id['p-c-late-vote']['decision'], 'late_vote_recorded')
        self.assertEqual(by_id['p-c-late-vote-again']['decision'], 'slot_already_voted')
        settled = by_id['p-settle-p']
        self.assertEqual(settled['decision'], 'written')
        self.assertEqual((settled['proposal'], settled['value']), ('P', '2'))
        self.assertEqual(by_id['p-new-q-denied']['decision'], 'position_already_settled')
        readable = by_id['p-read']
        self.assertEqual((readable['proposal'], readable['value']), ('P', '2'))
        self.assertEqual(replay(self.path)['verified'], True)

    def test_fixture_is_reentrant_across_interrupted_runs(self):
        steps = json.loads((FIXTURES / 'register_handover_fulfill.json').read_text(encoding='utf-8'))
        first = [act(self.db, s['subsystem'], {**s['command'], 'event_id': s['event_id']}) for s in steps]
        second = [act(self.db, s['subsystem'], {**s['command'], 'event_id': s['event_id']}) for s in steps]
        self.assertEqual(decisions(first), decisions(second))
        self.assertEqual(one(self.db, 'SELECT COUNT(*) AS n FROM register_writes')['n'], 1)


class FrozenScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'frozen.sqlite')
        self.db = connect(self.path)
        self.n = 0

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def send(self, module, **values):
        self.n += 1
        return act(self.db, module, dict(event_id=f'fr-{self.n}', **values))

    def test_reconnection_removal_and_same_named_new_node_keep_old_vote_power(self):
        for node in ('A', 'B', 'C'):
            self.send('replicas', op='add', epoch='old', node=node)
        for node in ('D', 'E', 'F'):
            self.send('replicas', op='add', epoch='new', node=node)
        self.send('register', op='vote', epoch='old', slot=7, node='A', proposal='P', value='2')
        # Freeze happened with three voters: quorum 2, even after topology churn.
        self.send('replicas', op='online', epoch='old', node='A', online=False)
        self.send('replicas', op='online', epoch='old', node='A', online=True)
        self.send('replicas', op='remove', epoch='old', node='C')
        self.send('replicas', op='add', epoch='new', node='C')
        scope = one(self.db, 'SELECT * FROM register_slot_scopes WHERE epoch=? AND slot=?', ('old', 7))
        self.assertEqual((json.loads(scope['voters']), scope['quorum']), (['A', 'B', 'C'], 2))
        # B's ballot plus A's still elects P under the frozen threshold.
        self.send('replicas', op='handover', epoch='old', new_epoch='new', mode='fulfill')
        self.assertEqual(self.send('register', op='vote', epoch='old', slot=7,
                                   node='B', proposal='P', value='2')['decision'], 'vote_recorded')
        result = self.send('register', op='apply', epoch='old', slot=7, proposal='P', value='2')
        self.assertEqual(result['decision'], 'written')
        self.assertEqual(result['votes'], 2)
        self.assertEqual(replay(self.path)['verified'], True)


class HandoverGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'guard.sqlite')
        self.db = connect(self.path)
        self.n = 0

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def send(self, module, **values):
        self.n += 1
        return act(self.db, module, dict(event_id=f'g-{self.n}', **values))

    def test_lost_client_receipt_is_reissued_without_second_write(self):
        for node in ('A', 'B', 'C'):
            self.send('replicas', op='add', epoch='old', node=node)
        for node in ('D', 'E', 'F'):
            self.send('replicas', op='add', epoch='new', node=node)
        write = dict(slot=7, proposal='P', value='2')
        self.send('register', op='vote', epoch='old', node='A', **write)
        self.send('register', op='vote', epoch='old', node='B', **write)
        first = self.send('register', op='apply', epoch='old', **write)
        # The controller never delivered the first receipt; the client retries
        # with a brand new event id and must get the same receipt, no new row.
        self.n += 1
        retry = act(self.db, 'register', dict(event_id=f'g-{self.n}', op='apply', epoch='old', **write))
        self.assertEqual(first['decision'], 'written')
        self.assertEqual(retry['decision'], 'slot_already_applied')
        self.assertEqual(first['receipt'], retry['receipt'])
        self.assertEqual(one(self.db, 'SELECT COUNT(*) AS n FROM register_writes')['n'], 1)

    def test_handover_mode_and_target_are_guarded(self):
        for node in ('A', 'B'):
            self.send('replicas', op='add', epoch='old', node=node)
        for node in ('D', 'E'):
            self.send('replicas', op='add', epoch='new', node=node)
        self.assertEqual(self.send('replicas', op='handover', epoch='old',
                                   new_epoch='new', mode='bogus')['decision'], 'invalid_handover')
        self.assertEqual(self.send('replicas', op='handover', epoch='old',
                                   new_epoch='old', mode='fulfill')['decision'], 'invalid_handover')
        self.send('replicas', op='handover', epoch='old', new_epoch='new', mode='fulfill')
        self.assertEqual(self.send('replicas', op='handover', epoch='old',
                                   new_epoch='new', mode='withdraw')['decision'], 'handover_conflict')
        self.assertEqual(self.send('replicas', op='seal',
                                   epoch='unknown')['decision'], 'handover_missing')
        self.assertEqual(self.send('replicas', op='complete', epoch='old',
                                   new_epoch='other')['decision'], 'handover_missing')


class CliHandoverTest(unittest.TestCase):
    def test_cli_runs_envelope_fixture_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / 'cli.sqlite')
            run = subprocess.run(
                [sys.executable, '-B', '-m', 'sloattrib', 'register', '--db', db,
                 '--fixture', str(FIXTURES / 'register_handover_withdraw.json')],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            payload = json.loads(run.stdout)
            wrapped = payload[0]['results']
            self.assertEqual(wrapped[0]['subsystem'], 'replicas')
            self.assertTrue(any(item['result'].get('decision') == 'written'
                                and item['result'].get('value') == '9' for item in wrapped))
            check = subprocess.run(
                [sys.executable, '-B', '-m', 'sloattrib', 'replay', '--db', db],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertTrue(json.loads(check.stdout)['verified'])


if __name__ == '__main__':
    unittest.main()

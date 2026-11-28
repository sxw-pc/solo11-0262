"""Synthetic switch-over runbook regressions.

These tests drive real, stepwise field evidence: queued valve commands,
actuator starts, and independently timed arrivals. The same commands are
replayed in two worlds whose actual arrival order differs, and the replayable
evidence must explain which racks can still accept requests at every tick.
"""
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from sloattrib.store import act, connect, many, one, replay


def build_site(db, prefix):
    """P and K, one unit each. P has A and B valves, K one isolated A branch."""
    def send(module, op, **values):
        send.seq += 1
        return act(db, module, dict(event_id=f'{prefix}-e{send.seq}', op=op, **values))
    send.seq = 0
    send('coolant', 'pump', id='P', units=1)
    send('coolant', 'pump', id='K', units=1)
    for direction in ('supply', 'return'):
        send('coolant', 'valve', id=f'PA-{direction}', pump='P', rack='A', direction=direction)
        send('coolant', 'valve', id=f'PB-{direction}', pump='P', rack='B', direction=direction)
        send('coolant', 'valve', id=f'KA-{direction}', pump='K', rack='A', direction=direction)
    # K has exactly one pre-registered isolated emergency branch to A. It is
    # not authorised yet, so it cannot serve while pumps share a rack.
    send('coolant', 'register_branch', id='ka-branch', pump='K', rack='A')
    # Initial field: only the P->A loop is physically open, return first.
    send('coolant', 'actuate', id='PA-return', open=True)
    send('coolant', 'actuate', id='PA-supply', open=True)
    return send


def queue_open(send, cmd_id, valve, tick):
    return send('coolant', 'queue', id=cmd_id, valve=valve, open=True, tick=tick)


def queue_close(send, cmd_id, valve, tick):
    return send('coolant', 'queue', id=cmd_id, valve=valve, open=False, tick=tick)


def start(send, cmd_id, tick):
    return send('coolant', 'start', id=cmd_id, tick=tick)


def arrive(send, cmd_id, tick):
    return send('coolant', 'arrive', id=cmd_id, tick=tick)


class BypassOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'bypass.sqlite')
        self.db = connect(self.path)
        self.send = build_site(self.db, 'bp')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_bypass_keeps_a_served_throughout_and_lands_b_on_p(self):
        send = self.send
        # The in-between A request must be really served by K.
        self.assertEqual(send('rack', 'serve', rack='A', uid='req-0', tick=0)['pump'], 'P')
        self.assertEqual(send('workorder', 'open', id='wo', kind='bypass', main_pump='P',
                              emergency_pump='K', rack_a='A', rack_b='B', tick=1)['stage'],
                         'authorised')
        send('coolant', 'grant_branch', id='grant-ka', pump='K', rack='A', actor='safety')
        # Issued but not arrived emergency valves cannot fake continuity.
        queue_open(send, 'q-ka-r', 'KA-return', 2)
        queue_open(send, 'q-ka-s', 'KA-supply', 2)
        start(send, 'q-ka-r', 3)
        start(send, 'q-ka-s', 3)
        self.assertEqual(send('workorder', 'advance', id='wo', tick=3)['decision'], 'stage_not_ready')
        # Real arrivals, return before supply so K's protection never trips.
        arrive(send, 'q-ka-r', 4)
        arrive(send, 'q-ka-s', 5)
        self.assertEqual(send('coolant', 'validate_branch', id='v-ka', pump='K', rack='A'),
                         {'decision': 'branch_validated'})
        self.assertEqual(send('workorder', 'advance', id='wo', tick=5)['stage'],
                         'emergency_validated')
        # Both pumps physically share A now; both stay within one unit.
        self.assertEqual(send('workorder', 'advance', id='wo', tick=6)['stage'],
                         'a_on_emergency')
        self.assertEqual(send('rack', 'serve', rack='A', uid='req-mid', tick=6)['pump'], 'K')
        # Unload P from A (supply closed first; closing cannot trip protection).
        queue_close(send, 'q-pa-s', 'PA-supply', 7)
        queue_close(send, 'q-pa-r', 'PA-return', 7)
        start(send, 'q-pa-s', 8)
        start(send, 'q-pa-r', 8)
        arrive(send, 'q-pa-s', 9)
        arrive(send, 'q-pa-r', 10)
        self.assertEqual(send('workorder', 'advance', id='wo', tick=10)['stage'], 'p_unloaded')
        self.assertEqual(send('rack', 'serve', rack='A', uid='req-mid2', tick=10)['pump'], 'K')
        # P onto B, return arrives before supply.
        queue_open(send, 'q-pb-r', 'PB-return', 11)
        queue_open(send, 'q-pb-s', 'PB-supply', 11)
        start(send, 'q-pb-r', 12)
        start(send, 'q-pb-s', 12)
        arrive(send, 'q-pb-r', 13)
        arrive(send, 'q-pb-s', 14)
        self.assertEqual(send('workorder', 'advance', id='wo', tick=14)['stage'], 'b_connected')
        self.assertEqual(send('rack', 'serve', rack='B', uid='req-b', tick=14)['pump'], 'P')
        self.assertEqual(send('rack', 'serve', rack='A', uid='req-a3', tick=14)['pump'], 'K')
        self.assertEqual(send('workorder', 'advance', id='wo', tick=15),
                         {'decision': 'work_order_completed', 'stage': 'completed'})
        attribution = {row['uid']: (row['rack'], row['pump'])
                       for row in many(self.db, 'SELECT uid, rack, pump FROM cooled_requests ORDER BY tick')}
        self.assertEqual(attribution, {
            'req-0': ('A', 'P'),
            'req-mid': ('A', 'K'),
            'req-mid2': ('A', 'K'),
            'req-b': ('B', 'P'),
            'req-a3': ('A', 'K'),
        })
        self.db.close()
        result = replay(self.path)
        self.assertTrue(result['verified'], result)
        with closing(connect(self.path)) as rebuilt:
            rebuilt_rows = {row['uid']: (row['rack'], row['pump'])
                            for row in many(rebuilt, 'SELECT uid, rack, pump FROM cooled_requests ORDER BY tick')}
        self.assertEqual(rebuilt_rows, attribution)

    def test_queued_but_not_arrived_emergency_valves_cannot_claim_continuity(self):
        send = self.send
        send('coolant', 'grant_branch', id='grant-ka', pump='K', rack='A', actor='safety')
        queue_open(send, 'q-ka-r', 'KA-return', 2)
        queue_open(send, 'q-ka-s', 'KA-supply', 2)
        # Only commands are queued; no arrival receipts exist.
        self.assertEqual(send('coolant', 'validate_branch', id='v-ka', pump='K', rack='A')['decision'],
                         'branch_not_ready')
        self.assertEqual(send('rack', 'serve', rack='A', uid='still-p', tick=3)['pump'], 'P')
        # Starting the actuators is still not a physical valve position.
        start(send, 'q-ka-r', 4)
        start(send, 'q-ka-s', 4)
        self.assertEqual(send('coolant', 'validate_branch', id='v-ka2', pump='K', rack='A')['decision'],
                         'branch_not_ready')
        self.assertEqual(send('rack', 'serve', rack='A', uid='still-p2', tick=4)['pump'], 'P')


class DowntimeOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'downtime.sqlite')
        self.db = connect(self.path)
        self.send = build_site(self.db, 'dt')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_downtime_blocks_a_and_records_real_no_service_window(self):
        send = self.send
        self.assertEqual(send('rack', 'serve', rack='A', uid='req-0', tick=0)['pump'], 'P')
        send('workorder', 'open', id='wo', kind='downtime', main_pump='P',
             rack_a='A', rack_b='B', tick=1)
        send('rack', 'drain', id='drain-a', rack='A', reason='switch_over', tick=2)
        self.assertEqual(send('workorder', 'advance', id='wo', tick=2)['stage'], 'a_drained')
        # The in-between A request is really rejected with block evidence.
        self.assertEqual(send('rack', 'serve', rack='A', uid='req-mid', tick=3),
                         {'decision': 'request_blocked'})
        # Unload P from A.
        queue_close(send, 'q-pa-s', 'PA-supply', 3)
        queue_close(send, 'q-pa-r', 'PA-return', 3)
        start(send, 'q-pa-s', 4)
        start(send, 'q-pa-r', 4)
        arrive(send, 'q-pa-s', 5)
        arrive(send, 'q-pa-r', 6)
        self.assertEqual(send('workorder', 'advance', id='wo', tick=6)['stage'], 'p_unloaded')
        self.assertEqual(send('rack', 'serve', rack='A', uid='req-mid2', tick=6),
                         {'decision': 'request_blocked'})
        rejection = one(self.db, "SELECT reason FROM rack_rejections WHERE uid='req-mid'")
        self.assertEqual(rejection['reason'], 'request_blocked')
        # P onto B safely.
        queue_open(send, 'q-pb-r', 'PB-return', 7)
        queue_open(send, 'q-pb-s', 'PB-supply', 7)
        start(send, 'q-pb-r', 8)
        start(send, 'q-pb-s', 8)
        arrive(send, 'q-pb-r', 9)
        arrive(send, 'q-pb-s', 10)
        self.assertEqual(send('workorder', 'advance', id='wo', tick=10)['stage'], 'b_connected')
        self.assertEqual(send('rack', 'serve', rack='B', uid='req-b', tick=10)['pump'], 'P')
        # The outage evidence is closed by an owner decision, not fabricated.
        self.assertEqual(send('workorder', 'advance', id='wo', tick=11)['decision'],
                         'stage_not_ready')
        send('rack', 'accept_outage', id='offline-a', rack='A', tick=11)
        self.assertEqual(send('workorder', 'advance', id='wo', tick=11)['stage'],
                         'outage_recorded')
        self.assertEqual(send('workorder', 'advance', id='wo', tick=12),
                         {'decision': 'work_order_completed', 'stage': 'completed'})
        outage = one(self.db, "SELECT * FROM rack_outages WHERE rack='A'")
        self.assertEqual((outage['start_tick'], outage['end_tick'], outage['status']),
                         (2, 11, 'accepted_offline'))
        self.db.close()
        result = replay(self.path)
        self.assertTrue(result['verified'], result)


class ArrivalOrderSwapTest(unittest.TestCase):
    def _world(self, prefix):
        tmp = tempfile.TemporaryDirectory()
        path = str(Path(tmp.name) / f'{prefix}.sqlite')
        db = connect(path)
        send = build_site(db, prefix)
        return tmp, path, db, send

    def _unload_p_from_a(self, send, base):
        queue_close(send, 'q-pa-s', 'PA-supply', base)
        queue_close(send, 'q-pa-r', 'PA-return', base)
        start(send, 'q-pa-s', base + 1)
        start(send, 'q-pa-r', base + 1)
        arrive(send, 'q-pa-s', base + 2)
        arrive(send, 'q-pa-r', base + 3)

    def _issue_pb_commands_same_order(self, send, base):
        # Identical dispatch order on both sites: return queued/started first.
        queue_open(send, 'q-pb-r', 'PB-return', base)
        queue_open(send, 'q-pb-s', 'PB-supply', base)
        start(send, 'q-pb-r', base + 1)
        start(send, 'q-pb-s', base + 1)

    def test_same_commands_same_final_positions_diverge_by_physical_arrival(self):
        tmp1, path1, db1, send1 = self._world('safe')
        tmp2, path2, db2, send2 = self._world('unsafe')
        for send in (send1, send2):
            self._unload_p_from_a(send, 2)
            self._issue_pb_commands_same_order(send, 6)
        # World 1: return reaches the field before the supply -> safe.
        self.assertEqual(arrive(send1, 'q-pb-r', 8)['decision'], 'arrived')
        self.assertEqual(arrive(send1, 'q-pb-s', 9)['decision'], 'arrived')
        self.assertEqual(send1('rack', 'serve', rack='B', uid='b-safe', tick=9)['pump'], 'P')
        # World 2: the supply arrives first -> protection trip, same end positions.
        self.assertEqual(arrive(send2, 'q-pb-s', 8),
                         {'decision': 'pump_tripped', 'pump': 'P'})
        self.assertEqual(arrive(send2, 'q-pb-r', 9)['decision'], 'arrived')
        self.assertEqual(one(db2, "SELECT status FROM cool_pumps WHERE id='P'")['status'], 'tripped')
        # The late return does not clear the trip: B cannot take requests.
        self.assertEqual(send2('rack', 'serve', rack='B', uid='b-unsafe', tick=10)['decision'],
                         'coolant_unavailable')
        # A reset without a permit, or while any hazard remains, is refused.
        self.assertEqual(send2('coolant', 'reset', id='reset-1', pump='P', permit='no-permit', tick=11),
                         {'decision': 'reset_permit_invalid'})
        send2('coolant', 'permit', id='permit-1', pump='P', actor='safety')
        self.assertEqual(send2('coolant', 'reset', id='reset-2', pump='P', permit='permit-1', tick=12),
                         {'decision': 'pump_reset'})
        # Permit is consumed; a second reset cannot reuse it.
        self.assertEqual(send2('coolant', 'reset', id='reset-3', pump='P', permit='permit-1', tick=13),
                         {'decision': 'reset_permit_invalid'})
        self.assertEqual(send2('rack', 'serve', rack='B', uid='b-after', tick=13)['pump'], 'P')
        db1.close()
        db2.close()
        for path in (path1, path2):
            result = replay(path)
            self.assertTrue(result['verified'], result)
        tmp1.cleanup()
        tmp2.cleanup()


class ProtectionTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'trip.sqlite')
        self.db = connect(self.path)
        self.send = build_site(self.db, 'tr')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_trip_persists_and_reset_requires_safe_field_and_permit(self):
        send = self.send
        # Emergency K onto A is attempted supply-first: K trips immediately.
        queue_open(send, 'q-ka-s', 'KA-supply', 2)
        queue_open(send, 'q-ka-r', 'KA-return', 2)
        start(send, 'q-ka-s', 3)
        start(send, 'q-ka-r', 3)
        self.assertEqual(arrive(send, 'q-ka-s', 4), {'decision': 'pump_tripped', 'pump': 'K'})
        # P still carries A; K's broken branch must not be validated.
        self.assertEqual(send('coolant', 'grant_branch', id='g', pump='K', rack='A', actor='s'),
                         {'decision': 'branch_granted'})
        self.assertEqual(send('coolant', 'validate_branch', id='v', pump='K', rack='A')['decision'],
                         'branch_not_ready')
        send('coolant', 'permit', id='perm', pump='K', actor='safety')
        # While the pressurised supply stays open with no matching return, the
        # field is unsafe and even a permitted reset is refused.
        self.assertEqual(send('coolant', 'reset', id='r0', pump='K', permit='perm', tick=5),
                         {'decision': 'reset_unsafe'})
        # The late return cannot itself clear the protection trip.
        arrive(send, 'q-ka-r', 6)
        self.assertEqual(one(self.db, "SELECT status FROM cool_pumps WHERE id='K'")['status'], 'tripped')
        # Close the hazard (supply closed), then the permitted reset works.
        queue_close(send, 'q-ka-s-off', 'KA-supply', 7)
        start(send, 'q-ka-s-off', 8)
        arrive(send, 'q-ka-s-off', 9)
        self.assertEqual(send('coolant', 'reset', id='r1', pump='K', permit='perm', tick=10),
                         {'decision': 'pump_reset'})
        self.assertEqual(one(self.db, "SELECT status FROM cool_trips WHERE pump='K'")['status'],
                         'cleared')


class EmergencyAuthorizationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'auth.sqlite')
        self.db = connect(self.path)
        self.send = build_site(self.db, 'au')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_unauthorised_emergency_sharing_is_excluded(self):
        send = self.send
        # K physically opens a loop to A without any branch grant; P is still
        # on A. The unauthorised extra pump must not take the rack.
        queue_open(send, 'q-ka-r', 'KA-return', 2)
        queue_open(send, 'q-ka-s', 'KA-supply', 2)
        start(send, 'q-ka-r', 3)
        start(send, 'q-ka-s', 3)
        arrive(send, 'q-ka-r', 4)
        arrive(send, 'q-ka-s', 5)
        self.assertEqual(send('rack', 'serve', rack='A', uid='a1', tick=5)['pump'], 'P')
        # Granted but not validated is still excluded.
        send('coolant', 'grant_branch', id='g', pump='K', rack='A', actor='safety')
        self.assertEqual(send('rack', 'serve', rack='A', uid='a2', tick=6)['pump'], 'P')
        self.assertEqual(send('coolant', 'validate_branch', id='v', pump='K', rack='A'),
                         {'decision': 'branch_validated'})
        self.assertEqual(send('rack', 'serve', rack='A', uid='a3', tick=7)['pump'], 'K')

    def test_oversubscribed_pump_loses_eligibility_on_every_rack(self):
        send = self.send
        # K validated on A first; then P would carry both A and B (two loops,
        # one unit). Every loop on P is disqualified while it is over capacity.
        send('coolant', 'grant_branch', id='g', pump='K', rack='A', actor='safety')
        queue_open(send, 'q-ka-r', 'KA-return', 2)
        queue_open(send, 'q-ka-s', 'KA-supply', 2)
        start(send, 'q-ka-r', 3)
        start(send, 'q-ka-s', 3)
        arrive(send, 'q-ka-r', 4)
        arrive(send, 'q-ka-s', 5)
        send('coolant', 'validate_branch', id='v', pump='K', rack='A')
        send('coolant', 'actuate', id='PB-return', open=True)
        send('coolant', 'actuate', id='PB-supply', open=True)
        # P now has complete loops to A and B but only one unit.
        self.assertEqual(send('rack', 'serve', rack='B', uid='b1', tick=6)['decision'],
                         'coolant_unavailable')
        self.assertEqual(send('rack', 'serve', rack='A', uid='a1', tick=6)['pump'], 'K')


class ReentrancyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 're.sqlite')
        self.db = connect(self.path)
        self.send = build_site(self.db, 're')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_late_start_receipt_and_duplicate_events_keep_one_physical_fact(self):
        send = self.send
        queue_open(send, 'q-pb-r', 'PB-return', 2)
        queue_open(send, 'q-pb-s', 'PB-supply', 2)
        # Arrival comes before the start receipt (out-of-order field evidence).
        self.assertEqual(arrive(send, 'q-pb-r', 3)['decision'], 'arrived')
        self.assertEqual(start(send, 'q-pb-r', 4)['decision'], 'phase_superseded')
        arrive(send, 'q-pb-s', 5)
        # Re-delivering receipts is idempotent and leaves one valve state.
        self.assertEqual(arrive(send, 'q-pb-r', 6)['decision'], 'already_arrived')
        self.assertEqual(arrive(send, 'q-pb-s', 7)['decision'], 'already_arrived')
        self.assertEqual(one(self.db, "SELECT COUNT(*) n FROM cool_valves WHERE opened=1")['n'], 4)
        # P is over capacity (A and B both open) until A is unloaded.
        self.assertEqual(send('rack', 'serve', rack='B', uid='b-wait', tick=7)['decision'],
                         'coolant_unavailable')
        queue_close(send, 'q-pa-s', 'PA-supply', 8)
        queue_close(send, 'q-pa-r', 'PA-return', 8)
        arrive(send, 'q-pa-s', 9)
        arrive(send, 'q-pa-r', 10)
        self.assertEqual(send('rack', 'serve', rack='B', uid='b-ok', tick=10)['pump'], 'P')
        # Re-driving the identical event stream yields identical decisions.
        self.db.close()
        result = replay(self.path)
        self.assertTrue(result['verified'], result)

    def test_work_order_advance_is_reentrant_against_partial_field_state(self):
        send = self.send
        send('workorder', 'open', id='wo', kind='downtime', main_pump='P',
             rack_a='A', rack_b='B', tick=1)
        # Repeated advance before any field move stays at the same stage.
        for tick in (2, 3):
            self.assertEqual(send('workorder', 'advance', id='wo', tick=tick)['decision'],
                             'stage_not_ready')
        send('rack', 'drain', id='d', rack='A', reason='switch', tick=4)
        self.assertEqual(send('workorder', 'advance', id='wo', tick=5)['stage'], 'a_drained')
        # Duplicate advance after the fact does not insert extra progress rows.
        self.assertEqual(send('workorder', 'advance', id='wo', tick=6)['decision'],
                         'stage_not_ready')
        self.assertEqual(one(self.db, "SELECT COUNT(*) n FROM work_order_progress")['n'], 2)

    def test_rejection_evidence_replays_original_verdict_not_a_success(self):
        send = self.send
        # A request that arrives while no loop is eligible is rejected.
        send('rack', 'drain', id='d', rack='A', reason='switch', tick=1)
        self.assertEqual(send('rack', 'serve', rack='A', uid='lost', tick=2),
                         {'decision': 'request_blocked'})
        self.db.close()
        result = replay(self.path)
        self.assertTrue(result['verified'], result)
        # Even after cooling physically returns, the historical request keeps
        # its recorded rejection: nobody may manufacture a served trajectory.
        with closing(connect(self.path)) as rebuilt:
            again = act(rebuilt, 'rack',
                        {'event_id': 'forged-redrive', 'op': 'serve', 'rack': 'A',
                         'uid': 'lost', 'tick': 9})
        self.assertEqual(again, {'decision': 'request_blocked'})
        check = connect(self.path)
        try:
            self.assertIsNone(one(check, "SELECT 1 FROM cooled_requests WHERE uid='lost'"))
        finally:
            check.close()


if __name__ == '__main__':
    unittest.main()

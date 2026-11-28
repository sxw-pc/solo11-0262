"""Address recycling handoff regressions.

Synthetic site shared by every scenario:
  t=0  checkout published at A, leaf client caches the answer until t=5
  t=2  checkout moves to B (A remains usable under the old generation)
       and A is recycled toward the new batch service
  t=3  a batch client holds a marker minted only for this A lease,
       another same-address client holds only a past-round marker

Two delivery routes are exercised with itemized commands:
  * exclusive  — plain listener refuses new batch until old rights are
                 proven exhausted;
  * compatible — compat entry serves current-round batch immediately while
                 unmarked connections keep reaching checkout.
"""
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from sloattrib.store import act, connect, many, one, replay


class Sender:
    def __init__(self, db):
        self.db = db
        self.count = 0

    def __call__(self, module, op, /, **values):
        self.count += 1
        eid = values.pop('event_id', f'evt-{self.count}')
        return act(self.db, module, dict(event_id=eid, op=op, **values))


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'handoff.sqlite')
        self.db = connect(self.path)
        self.send = Sender(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def deliveries(self):
        return many(self.db, 'SELECT client,address,backend,generation,marker '
                             'FROM gateway_deliveries ORDER BY rowid')

    def denials(self):
        return many(self.db, 'SELECT client,reason FROM gateway_denials ORDER BY rowid')

    def build_site(self, mode='exclusive'):
        """Publish/checkout move through t=2; returns the two lease markers."""
        s = self.send
        s('gateway', 'listen', address='A', backend='checkout-a')
        s('gateway', 'listen', address='B', backend='checkout-b')
        pub = s('dns', 'publish', name='checkout', address='A',
                identity='checkout', ttl=5, at=0)
        gen_one, old_marker = pub['generation'], pub['marker']
        leaf = s('dns', 'lookup', name='checkout', client='leaf', at=0)
        self.assertEqual((leaf['address'], leaf['expires']), ('A', 5))
        s('dns', 'publish', name='checkout', address='B',
          identity='checkout', ttl=5, at=2)
        reclaim = s('dns', 'recycle', reclaim='rc-A', address='A',
                    old_owner='checkout', new_owner='batch', mode=mode, at=2)
        self.assertEqual(reclaim['old_generation'], gen_one)
        new_gen, new_marker = reclaim['new_generation'], reclaim['marker']
        s('dns', 'publish', name='batch', address='A', identity='batch',
          ttl=5, at=2)
        batch_answer = s('dns', 'lookup', name='batch', client='batch-new', at=3)
        self.assertEqual((batch_answer['generation'], batch_answer['marker']),
                         (new_gen, new_marker))
        return gen_one, old_marker, new_gen, new_marker

    def test_exclusive_route_waits_for_exhaustion_evidence(self):
        s = self.send
        gen_one, old_marker, new_gen, new_marker = self.build_site()
        # Plain exclusive listener for the new owner on A.
        self.assertEqual(s('gateway', 'listen', address='A', backend='batch-a')
                         ['decision'], 'listening')

        # t=3: no proof of old-rights exhaustion yet, the leaf's unmarked
        # connection is refused rather than misdelivered to batch.
        self.assertEqual(s('gateway', 'deliver', name='checkout', client='leaf',
                           at=3)['decision'], 'reclaim_pending')
        # Current-round batch marker is likewise not accepted yet.
        self.assertEqual(s('gateway', 'deliver', client='batch-new', address='A',
                           marker=new_marker, at=3)['decision'], 'reclaim_pending')
        # A late/replayed credential from the previous A allocation can never
        # reach the new owner.
        self.assertEqual(s('gateway', 'deliver', client='stale', address='A',
                           marker=old_marker, at=3)['decision'], 'stale_lease')
        # Central tables cannot conjure exhaustion: leaf's cache is still live.
        exam = s('dns', 'examine_reclaim', reclaim='rc-A', at=3)
        self.assertEqual(exam['decision'], 'rights_outstanding')
        self.assertIn('cache:leaf:checkout', exam['branches'])

        # The leaf branch surrenders; no in-flight forwards to enumerate.
        withdrawal = s('dns', 'withdraw', withdrawal_id='w-leaf', name='checkout',
                       client='leaf', generation=gen_one, at=4, covers=[])
        self.assertEqual(withdrawal['decision'], 'branch_released')
        sealed = s('dns', 'examine_reclaim', reclaim='rc-A', at=4)
        self.assertEqual((sealed['decision'], sealed['basis']),
                         ('reclaim_sealed', 'early_surrender'))
        # Examination is idempotent and re-entrant.
        self.assertEqual(s('dns', 'examine_reclaim', reclaim='rc-A', at=4)
                         ['decision'], 'reclaim_sealed')

        # Exclusive delivery now actually happens for both services.
        batch = s('gateway', 'deliver', client='batch-new', address='A',
                  marker=new_marker, at=4)
        self.assertEqual((batch['decision'], batch['backend']),
                         ('delivered', 'batch-a'))
        # A fresh client resolves checkout's current record (B), then delivers.
        fresh_lookup = s('dns', 'lookup', name='checkout', client='fresh', at=4)
        self.assertEqual(fresh_lookup['address'], 'B')
        fresh = s('gateway', 'deliver', name='checkout', client='fresh', at=4)
        self.assertEqual((fresh['decision'], fresh['backend'], fresh['address']),
                         ('delivered', 'checkout-b', 'B'))
        checkouts = [d for d in self.deliveries() if d['backend'] == 'checkout-b']
        self.assertEqual(len(checkouts), 1)
        # Old-generation credential arriving after handoff is still refused.
        self.assertEqual(s('gateway', 'deliver', client='stale', address='A',
                           marker=old_marker, at=6)['decision'], 'stale_lease')
        self.assertEqual(replay(self.path)['verified'], True)

    def test_compat_entry_serves_batch_while_keeping_unmarked_on_checkout(self):
        s = self.send
        gen_one, old_marker, new_gen, new_marker = self.build_site('compatible')
        entry = s('gateway', 'listen', address='A', backend='batch-a',
                  kind='compat', entry_reclaim='rc-A')
        self.assertEqual(entry['decision'], 'listening')
        # A compat entry cannot be registered without its reclaim evidence.
        self.assertEqual(s('gateway', 'listen', address='A', backend='batch-a',
                           kind='compat')['decision'], 'entry_reclaim_required')

        # t=3: current-round batch works, unmarked checkout still reaches the
        # old owner, past-round markers are rejected.
        batch = s('gateway', 'deliver', client='batch-new', address='A',
                  marker=new_marker, at=3)
        self.assertEqual((batch['decision'], batch['backend']),
                         ('delivered', 'batch-a'))
        legacy = s('gateway', 'deliver', name='checkout', client='leaf', at=3)
        self.assertEqual((legacy['decision'], legacy['backend'], legacy['address']),
                         ('delivered', 'checkout-b', 'A'))
        self.assertEqual(s('gateway', 'deliver', client='stale', address='A',
                           marker=old_marker, at=3)['decision'], 'stale_lease')

        # Once the old branch returns its rights, the entry can complete the
        # exclusive handoff itself.
        s('dns', 'withdraw', withdrawal_id='w-leaf', name='checkout',
          client='leaf', generation=gen_one, at=4, covers=[])
        self.assertEqual(s('dns', 'examine_reclaim', reclaim='rc-A', at=4)
                         ['basis'], 'early_surrender')
        self.assertEqual(s('gateway', 'promote', address='A')['decision'],
                         'exclusive_listening')
        promoted = s('gateway', 'deliver', client='batch-new', address='A',
                     marker=new_marker, at=5)
        self.assertEqual((promoted['decision'], promoted['backend']),
                         ('delivered', 'batch-a'))
        # No unmarked old-rights traffic can land after promotion: a late
        # client has no live cache and must re-resolve before any delivery.
        self.assertEqual(s('gateway', 'deliver', name='checkout', client='late',
                           at=5)['decision'], 'lookup_required')
        backends = {d['backend'] for d in self.deliveries()}
        self.assertEqual(backends, {'batch-a', 'checkout-b'})
        self.assertEqual(replay(self.path)['verified'], True)

    def test_recursive_forward_caps_downstream_validity_at_upstream_deadline(self):
        s = self.send
        s('gateway', 'listen', address='A', backend='checkout-a')
        s('gateway', 'listen', address='B', backend='checkout-b')
        s('dns', 'publish', name='checkout', address='A', identity='checkout',
          ttl=5, at=0)
        s('dns', 'lookup', name='checkout', client='leaf', at=0)
        # Upstream answer remains valid until t=5; it is relayed after the move.
        s('dns', 'publish', name='checkout', address='B', identity='checkout',
          ttl=5, at=2)
        s('dns', 'recycle', reclaim='rc-relay', address='A', old_owner='checkout',
          new_owner='batch', mode='compatible', at=2)
        emitted = s('dns', 'emit_forward', forward_id='f1', name='checkout',
                    upstream='leaf', downstream='relay', at=3)
        self.assertEqual(emitted['upstream_expires'], 5)

        # A central table looking only at dns_caches sees no relay branch yet
        # and would mechanically declare A recyclable; the open in-flight
        # forward must keep the reclaim open regardless. Neither can the leaf
        # branch surrender while its answer is still travelling unacknowledged.
        self.assertEqual(s('dns', 'withdraw', withdrawal_id='w-leaf-blocked',
                          name='checkout', client='leaf', generation=1, at=3,
                          covers=[])['decision'], 'in_flight_uncovered')
        self.assertEqual(s('dns', 'withdraw', withdrawal_id='w-leaf-early',
                          name='checkout', client='leaf', generation=1, at=3,
                          covers=['f1'])['decision'], 'in_flight_uncovered')
        exam = s('dns', 'examine_reclaim', reclaim='rc-relay', at=3)
        self.assertEqual(exam['decision'], 'rights_outstanding')
        self.assertIn('forward:f1', exam['branches'])

        # Acceptance after the upstream's absolute deadline is impossible.
        self.assertEqual(s('dns', 'accept_forward', forward_id='f1',
                           identity='checkout', ttl=10, at=6)['decision'],
                         'forward_answer_dead')
        # Accepted at t=4 with a 10-tick request: the cap bites at t=5.
        accepted = s('dns', 'accept_forward', forward_id='f1',
                     identity='checkout', ttl=10, at=4)
        self.assertEqual((accepted['decision'], accepted['expires']),
                         ('forward_accepted', 5))
        relay_cache = one(self.db, "SELECT expires,provenance,generation FROM "
                                   "dns_caches WHERE client='relay' AND name='checkout'")
        self.assertEqual((relay_cache['expires'], relay_cache['provenance']),
                         (5, 'forward'))

        # Responsibility has moved to the relay branch; the leaf can now
        # surrender, enumerating the forward it actually shut down, but only
        # gets a partial acknowledgement while the successor stays alive.
        leaf_release = s('dns', 'withdraw', withdrawal_id='w-leaf',
                         name='checkout', client='leaf', generation=1, at=4,
                         covers=['f1'])
        self.assertEqual(leaf_release['decision'], 'partial_surrender')

        s('gateway', 'listen', address='A', backend='batch-a', kind='compat',
          entry_reclaim='rc-relay')
        # Short-term compatible handoff works while the relayed answer lives.
        # The unmarked connection on A is redirected to the old owner's own
        # listener at its current address B.
        legacy = s('gateway', 'deliver', name='checkout', client='relay', at=4)
        self.assertEqual((legacy['decision'], legacy['backend'], legacy['address']),
                         ('delivered', 'checkout-b', 'A'))
        # True exclusive handoff stays blocked: the relay-derived old connection
        # arrives later than the central cache's mechanical t=? observation.
        self.assertEqual(s('dns', 'examine_reclaim', reclaim='rc-relay', at=4)
                         ['decision'], 'rights_outstanding')
        s('dns', 'withdraw', withdrawal_id='w-relay', name='checkout',
          client='relay', generation=1, at=5, covers=[])
        sealed = s('dns', 'examine_reclaim', reclaim='rc-relay', at=5)
        self.assertEqual((sealed['decision'], sealed['basis']),
                         ('reclaim_sealed', 'natural_expiry'))
        s('gateway', 'promote', address='A')
        self.assertEqual(s('gateway', 'deliver', name='checkout', client='relay',
                           at=5)['decision'], 'lookup_required')
        self.assertEqual(replay(self.path)['verified'], True)

    def test_partial_acknowledgements_cannot_be_combined_into_a_proof(self):
        s = self.send
        s('gateway', 'listen', address='A', backend='checkout-a')
        s('dns', 'publish', name='checkout', address='A', identity='checkout',
          ttl=10, at=0)
        s('dns', 'lookup', name='checkout', client='leaf', at=0)
        s('dns', 'lookup', name='checkout', client='branch2', at=1)
        # leaf feeds branch2 through a forward; withdrawing leaf while that
        # successor is alive is only a partial acknowledgement.
        s('dns', 'emit_forward', forward_id='f2', name='checkout',
          upstream='leaf', downstream='branch2', at=2)
        s('dns', 'accept_forward', forward_id='f2', identity='checkout',
          ttl=5, at=2)
        s('dns', 'publish', name='checkout', address='B', identity='checkout',
          ttl=10, at=2)
        s('dns', 'recycle', reclaim='rc-partial', address='A',
          old_owner='checkout', new_owner='batch', mode='exclusive', at=2)

        partial = s('dns', 'withdraw', withdrawal_id='w1', name='checkout',
                    client='leaf', generation=1, at=3, covers=['f2'])
        self.assertEqual(partial['decision'], 'partial_surrender')
        # An interrupted recovery that replays the same acknowledgement must
        # not mutate state or invent a second proof.
        self.assertEqual(act(self.db, 'dns', dict(
            event_id='w1', op='withdraw', withdrawal_id='w1', name='checkout',
            client='leaf', generation=1, at=3, covers=['f2']))['decision'],
            'withdrawal_duplicate')
        self.assertEqual(s('dns', 'examine_reclaim', reclaim='rc-partial', at=3)
                         ['decision'], 'rights_outstanding')
        # The same command split across a reconnect cannot be synthesized:
        # branch2's withdrawal binds its own branch and generation.
        self.assertEqual(s('dns', 'withdraw', withdrawal_id='w2-bad',
                          name='checkout', client='branch2', generation=2,
                          at=4, covers=[])['decision'], 'generation_mismatch')
        full = s('dns', 'withdraw', withdrawal_id='w2', name='checkout',
                 client='branch2', generation=1, at=4, covers=[])
        self.assertEqual(full['decision'], 'branch_released')
        sealed = s('dns', 'examine_reclaim', reclaim='rc-partial', at=4)
        self.assertEqual((sealed['decision'], sealed['basis']),
                         ('reclaim_sealed', 'early_surrender'))
        self.assertEqual(replay(self.path)['verified'], True)

    def test_overlapping_reclaims_keep_credentials_generation_bound(self):
        s = self.send
        s('gateway', 'listen', address='A', backend='checkout-a')
        first = s('dns', 'publish', name='checkout', address='A',
                  identity='checkout', ttl=5, at=0)
        s('dns', 'lookup', name='checkout', client='leaf', at=0)
        s('dns', 'publish', name='checkout', address='B', identity='checkout',
          ttl=5, at=1)
        rc1 = s('dns', 'recycle', reclaim='rc1', address='A',
                old_owner='checkout', new_owner='batch', mode='exclusive', at=1)
        batch_one_marker = rc1['marker']
        s('dns', 'publish', name='batch', address='A', identity='batch',
          ttl=5, at=1)
        s('dns', 'withdraw', withdrawal_id='w1', name='checkout', client='leaf',
          generation=first['generation'], at=2, covers=[])
        self.assertEqual(s('dns', 'examine_reclaim', reclaim='rc1', at=2)
                         ['decision'], 'reclaim_sealed')
        # Second recycling round on A while the first's async work is done:
        # batch moves out, an analytics service becomes the next owner.
        s('dns', 'publish', name='batch', address='C', identity='batch',
          ttl=5, at=3)
        rc2 = s('dns', 'recycle', reclaim='rc2', address='A',
                old_owner='batch', new_owner='analytics', mode='compatible', at=3)
        analytics_marker = rc2['marker']
        self.assertNotEqual(batch_one_marker, analytics_marker)
        s('dns', 'publish', name='analytics', address='A', identity='analytics',
          ttl=5, at=3)
        s('gateway', 'listen', address='A', backend='analytics-a', kind='compat',
          entry_reclaim='rc2')

        # The prior-round batch marker is now a stale credential for A.
        self.assertEqual(s('gateway', 'deliver', client='old-batch', address='A',
                           marker=batch_one_marker, at=4)['decision'], 'stale_lease')
        current = s('gateway', 'deliver', client='analytics', address='A',
                    marker=analytics_marker, at=4)
        self.assertEqual(current['backend'], 'analytics-a')
        # A second open reclaim on the same address is rejected until rc2 seals.
        self.assertEqual(s('dns', 'recycle', reclaim='rc3', address='A',
                           old_owner='analytics', new_owner='other',
                           mode='exclusive', at=4)['decision'],
                         'reclaim_already_open')
        self.assertEqual(replay(self.path)['verified'], True)

    def test_returning_owner_gets_fresh_generation_and_dead_marker(self):
        s = self.send
        s('gateway', 'listen', address='A', backend='checkout-a')
        first = s('dns', 'publish', name='checkout', address='A',
                  identity='checkout', ttl=5, at=0)
        s('dns', 'lookup', name='checkout', client='leaf', at=0)
        s('dns', 'publish', name='checkout', address='B', identity='checkout',
          ttl=5, at=1)
        s('dns', 'recycle', reclaim='rc-out', address='A', old_owner='checkout',
          new_owner='batch', mode='exclusive', at=1)
        s('dns', 'publish', name='batch', address='A', identity='batch',
          ttl=5, at=1)
        s('dns', 'withdraw', withdrawal_id='w1', name='checkout', client='leaf',
          generation=first['generation'], at=2, covers=[])
        s('dns', 'examine_reclaim', reclaim='rc-out', at=2)
        # checkout later returns to A: a fresh round must mint a new marker,
        # never revive its generation-1 credential.
        s('dns', 'publish', name='batch', address='C', identity='batch',
          ttl=5, at=3)
        returning = s('dns', 'recycle', reclaim='rc-back', address='A',
                      old_owner='batch', new_owner='checkout',
                      mode='exclusive', at=3)
        s('dns', 'publish', name='checkout2', address='A', identity='checkout',
          ttl=5, at=3)
        self.assertNotEqual(returning['marker'], first['marker'])
        self.assertGreater(returning['new_generation'], first['generation'])
        s('gateway', 'listen', address='A', backend='checkout-again')
        # Before exhaustion evidence, the exclusive route holds new traffic.
        self.assertEqual(s('gateway', 'deliver', client='checkout', address='A',
                           marker=returning['marker'], at=4)['decision'],
                         'reclaim_pending')
        # No old-generation branches remain outstanding, so examination seals
        # the reclaim immediately (the old rights were already proven gone).
        self.assertEqual(s('dns', 'examine_reclaim', reclaim='rc-back', at=4)
                         ['decision'], 'reclaim_sealed')
        # The original round-1 marker is dead even though the same owner holds A.
        self.assertEqual(s('gateway', 'deliver', client='ghost', address='A',
                           marker=first['marker'], at=5)['decision'], 'stale_lease')
        live = s('gateway', 'deliver', client='checkout', address='A',
                 marker=returning['marker'], at=5)
        self.assertEqual((live['decision'], live['backend']),
                         ('delivered', 'checkout-again'))
        self.assertEqual(replay(self.path)['verified'], True)

    def test_disconnected_publishers_and_process_reentry(self):
        s = self.send
        gen_one, old_marker, new_gen, new_marker = self.build_site('compatible')
        s('gateway', 'listen', address='A', backend='batch-a', kind='compat',
          entry_reclaim='rc-A')
        # Simulate both publishers being offline: no new dns/gateway commands
        # are needed to keep serving, and every command is safe to resend.
        first = act(self.db, 'gateway', dict(
            event_id='dup-1', op='deliver', client='batch-new', address='A',
            marker=new_marker, at=3))
        again = act(self.db, 'gateway', dict(
            event_id='dup-1', op='deliver', client='batch-new', address='A',
            marker=new_marker, at=3))
        self.assertEqual(first, again)
        self.assertEqual(len([d for d in self.deliveries()
                              if d['client'] == 'batch-new']), 1)
        # Same event id with a different body is a conflict, not a delivery.
        self.assertEqual(act(self.db, 'gateway', dict(
            event_id='dup-1', op='deliver', client='impostor', address='A',
            marker=new_marker, at=3))['decision'], 'event_identity_conflict')
        # A fresh process reopens the database and replays every event from
        # the journal; routing decisions and table state reproduce exactly.
        result = replay(self.path)
        self.assertTrue(result['verified'], result)


if __name__ == '__main__':
    unittest.main()

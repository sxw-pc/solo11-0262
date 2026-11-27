"""Create and verify portable offline SLO accident evidence packages.

Two custody contracts share the same sealed digest envelope:

* ``full`` packages copy every business event that existed at sealing time.
  A reviewer can reopen the case without any hot-store access because the
  package contains explicit contemporaneous originals.
* ``projected`` packages anchor on a trusted hash-chain proof of the event
  prefix, carry only the originals selected into the case's review scope and
  describe the remaining prefix state as projections plus version bindings.
  Summarised originals may only be read through retrieval grants bound to the
  exact incident-time version; when that version can no longer be retrieved
  the review is reported as restricted instead of letting an old summary
  attest to a new fact.
"""
import hashlib
import json
import sqlite3
from contextlib import closing

from .store import act, connect, many, one

SCHEMA = '''
CREATE TABLE IF NOT EXISTS custody_packages(
    id TEXT PRIMARY KEY, case_id TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS custody_receipts(
    id TEXT PRIMARY KEY, case_id TEXT NOT NULL, digest TEXT NOT NULL, decision TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS custody_pins(
    package_id TEXT NOT NULL, case_id TEXT NOT NULL, event_id TEXT NOT NULL, digest TEXT NOT NULL,
    PRIMARY KEY(package_id, event_id)
);
CREATE TABLE IF NOT EXISTS custody_grants(
    id TEXT PRIMARY KEY, case_id TEXT NOT NULL, package_id TEXT NOT NULL,
    event_id TEXT NOT NULL, digest TEXT NOT NULL, version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS custody_trust(
    anchor TEXT PRIMARY KEY, chain_head TEXT NOT NULL
);
'''
SOURCE_MODULES = ('slo', 'cause', 'credit')
GENESIS = 'custody-genesis'
# Tables a projected package is allowed to describe as prefix state.
PROJECTION_TABLES = (
    'objectives', 'request_shards', 'windows', 'waivers', 'topology',
    'failure_samples', 'cause_cases', 'cause_disputes', 'credit_claims',
    'settlement_receipts', 'settlement_reversals',
)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def record_digest(record):
    """Digest of an original action as it appeared in the event log."""
    return digest({'subsystem': record['subsystem'], 'payload': record['payload'],
                   'result': record['result']})


def version_coords(record):
    """Target-version coordinates that pin an original to its incident facts.

    A replacement record sharing the same UID but pointing at another window,
    objective or topology generation produces different coordinates and is
    rejected even when its outer envelope is re-sealed coherently.
    """
    payload = record['payload']
    op = payload['op']
    coords = {'op': op, 'id': payload.get('id')}
    if op == 'objective':
        coords.update(service=payload['service'], category=payload['category'],
                      revision=payload['revision'])
    elif op == 'shard':
        coords.update(objective=payload['objective'], source=payload['source'],
                      generation=payload['generation'])
    elif op == 'seal':
        coords.update(objective=payload['objective'])
    elif op == 'waiver':
        coords.update(objective=payload['objective'])
    elif op == 'topology':
        coords.update(service=payload['service'], generation=payload['generation'])
    elif op == 'sample':
        coords.update(window=payload['window'], request_uid=payload['request_uid'],
                      source=payload['source'])
    elif op == 'decide':
        coords.update(window=payload['window'], topology=payload['topology'])
    elif op == 'dispute':
        coords.update(case=payload['case'], evidence=payload['evidence'])
    elif op == 'claim':
        coords.update(case=payload['case'], tenant=payload['tenant'], period=payload['period'])
    elif op == 'settle':
        coords.update(claim=payload['claim'])
    elif op == 'reverse':
        coords.update(receipt=payload['receipt'])
    return coords


def _event_rows(db):
    return many(db, "SELECT seq, subsystem, payload, result FROM events "
                    "WHERE subsystem IN ('slo','cause','credit') ORDER BY seq")


def _as_record(row):
    return {'subsystem': row['subsystem'], 'payload': json.loads(row['payload']),
            'result': json.loads(row['result'])}


def chain_head(records):
    """Fold the event log into a tamper-evident hash-chain head."""
    head = hashlib.sha256(GENESIS.encode('utf-8')).hexdigest()
    for record in records:
        head = digest({'prev': head, 'record': {'subsystem': record['subsystem'],
                                                'payload': record['payload'],
                                                'result': record['result']}})
    return head


def package(db, case_id):
    """Full original-text package (the original whole-history handoff)."""
    records = [_as_record(row) for row in _event_rows(db)]
    return {'case': case_id, 'contract': 'full', 'events': records}


def _classify_scope(db, case_id):
    """Return (in-scope event ids, metadata) for a case's review scope.

    The scope is derived from the business rows the case's review actually
    depends on at sealing time, not from every matching event ever seen:
    rejected samples and later topology generations that superseded the
    incident-time view are deliberately outside the contemporaneous scope.
    """
    case = one(db, 'SELECT * FROM cause_cases WHERE id=?', (case_id,))
    if not case:
        return None
    window = one(db, 'SELECT * FROM windows WHERE id=?', (case['window_id'],))
    objective_id = window['objective'] if window else None
    rows = _event_rows(db)
    sample_ids = {r['id'] for r in db.execute(
        'SELECT id FROM failure_samples WHERE window_id=?', (case['window_id'],))}
    dispute_ids = {r['id'] for r in db.execute(
        'SELECT id FROM cause_disputes WHERE case_id=?', (case_id,))}
    claim_ids = {r['id'] for r in db.execute(
        'SELECT id FROM credit_claims WHERE case_id=?', (case_id,))}
    receipt_ids = set()
    if claim_ids:
        marks = ','.join('?' for _ in claim_ids)
        receipt_ids = {r['id'] for r in db.execute(
            f'SELECT id FROM settlement_receipts WHERE claim_id IN ({marks})',
            tuple(claim_ids))}
    in_scope = set()
    for row in rows:
        payload = json.loads(row['payload'])
        op = payload.get('op')
        sub = row['subsystem']
        hit = False
        if sub == 'slo':
            if op == 'objective' and payload.get('id') == objective_id:
                hit = True
            elif op in ('shard', 'waiver') and payload.get('objective') == objective_id:
                hit = True
            elif op == 'seal' and payload.get('id') == case['window_id']:
                hit = True
        elif sub == 'cause':
            # Only the topology generation the case was decided against is
            # in scope; later generations are post-incident history.
            if op == 'topology' and payload.get('id') == case['topology_id'] \
                    and payload.get('generation') == case['topology_generation']:
                hit = True
            elif op == 'sample' and payload.get('id') in sample_ids:
                hit = True
            elif op == 'decide' and payload.get('id') == case_id:
                hit = True
            elif op == 'dispute' and payload.get('id') in dispute_ids:
                hit = True
            elif op == 'resolve' and payload.get('id') in dispute_ids:
                hit = True
        elif sub == 'credit':
            if op == 'claim' and payload.get('id') in claim_ids:
                hit = True
            elif op == 'settle' and payload.get('id') in receipt_ids:
                hit = True
            elif op == 'reverse' and payload.get('receipt') in receipt_ids:
                hit = True
        if hit:
            in_scope.add(row['seq'])
    return {'case': case, 'window': window, 'objective': objective_id,
            'rows': rows, 'in_scope': in_scope, 'claims': claim_ids,
            'receipts': receipt_ids, 'disputes': dispute_ids}


def _project_state(db, plan):
    """Select the prefix rows a reviewer needs for the scoped case."""
    case = plan['case']
    objective_id = plan['objective']
    projections = []

    def take(table, sql, args=()):
        for row in many(db, f'SELECT * FROM {table} WHERE {sql}', args):
            projections.append({'table': table, 'row': dict(row)})

    if objective_id:
        take('objectives', 'id=?', (objective_id,))
        take('request_shards', 'objective=?', (objective_id,))
        take('waivers', 'objective=?', (objective_id,))
    take('windows', 'id=?', (case['window_id'],))
    take('topology', 'id=?', (case['topology_id'],))
    take('failure_samples', 'window_id=?', (case['window_id'],))
    take('cause_cases', 'id=?', (case['id'],))
    take('cause_disputes', 'case_id=?', (case['id'],))
    take('credit_claims', 'case_id=?', (case['id'],))
    if plan['claims']:
        marks = ','.join('?' for _ in plan['claims'])
        take('settlement_receipts', f'claim_id IN ({marks})', tuple(plan['claims']))
    if plan['receipts']:
        marks = ','.join('?' for _ in plan['receipts'])
        take('settlement_reversals', f'receipt_id IN ({marks})', tuple(plan['receipts']))
    return projections


def projected_package(db, case_id, refs, grants, project=None):
    """Build a minimal package from a trusted prefix proof plus picked events.

    ``refs`` names originals kept as summary projections with retrieval
    grants; ``project`` may demote additional in-scope events to projections.
    Every demoted event must sit inside the natural review scope, and at
    least one original action remains in the package for live replay.
    """
    plan = _classify_scope(db, case_id)
    if not plan:
        return None
    rows = plan['rows']
    by_event_id = {json.loads(row['payload'])['event_id']: row for row in rows}
    project = list(dict.fromkeys([*refs, *(project or [])]))
    unknown = [event_id for event_id in project if event_id not in by_event_id]
    if unknown:
        return {'error': 'custody_scope_invalid', 'detail': 'unknown_ref', 'refs': unknown}
    not_scoped = [event_id for event_id in project
                  if by_event_id[event_id]['seq'] not in plan['in_scope']]
    if not_scoped:
        return {'error': 'custody_scope_invalid', 'detail': 'ref_outside_scope',
                'refs': not_scoped}
    grant_seen = set()
    for grant in grants:
        if grant['id'] in grant_seen or grant.get('event_id') not in refs:
            return {'error': 'custody_scope_invalid', 'detail': 'grant_invalid',
                    'grant': grant.get('id')}
        grant_seen.add(grant['id'])

    anchor_seq = max(by_event_id[event_id]['seq'] for event_id in project)
    included_rows = [row for row in rows
                     if row['seq'] in plan['in_scope'] and row['seq'] > anchor_seq]
    if not included_rows:
        return {'error': 'custody_scope_invalid', 'detail': 'no_original_action'}

    prefix_records = [_as_record(row) for row in rows if row['seq'] <= anchor_seq]
    anchor = chain_head(prefix_records)
    with closing(connect(':memory:')) as prefix_db:
        for record in prefix_records:
            act(prefix_db, record['subsystem'], record['payload'])
        projections = _project_state(prefix_db, plan)

    included = [_as_record(row) for row in included_rows]
    # Every demoted prefix event is version-bound, but only refs may carry
    # retrieval grants; plain projections are provable via the anchor alone.
    projected_records = [_as_record(by_event_id[event_id]) for event_id in project]
    bindings = []
    for record in [*included, *projected_records]:
        bindings.append({'event_id': record['payload']['event_id'],
                         'digest': record_digest(record),
                         'version': version_coords(record)})
    binding_index = {b['event_id']: b for b in bindings}
    retrieval = [{'id': grant['id'], 'event_id': grant['event_id'],
                  'digest': binding_index[grant['event_id']]['digest'],
                  'version': binding_index[grant['event_id']]['version']}
                 for grant in grants]
    bundle = {
        'case': case_id,
        'contract': 'projected',
        'package_id': None,
        'anchor_seq': anchor_seq,
        'anchor': anchor,
        'events': included,
        'projections': projections,
        'bindings': bindings,
        'retrieval': retrieval,
    }
    bundle['manifest'] = digest({'anchor': anchor, 'bindings': bindings,
                                 'projections': projections})
    return bundle


def _replay_records(records):
    target = connect(':memory:')
    for record in records:
        if not isinstance(record, dict) or record.get('subsystem') not in SOURCE_MODULES:
            target.close()
            return None
        try:
            outcome = act(target, record['subsystem'], record['payload'])
        except (KeyError, TypeError, ValueError, sqlite3.IntegrityError):
            target.close()
            return None
        if outcome != record.get('result'):
            target.close()
            return None
    return target


def verify_full(bundle):
    target = _replay_records(bundle['events'])
    if target is None:
        return None
    case = one(target, 'SELECT status FROM cause_cases WHERE id=?', (bundle['case'],))
    target.close()
    return case


def _load_projections(target, projections):
    allowed = {name: {r['name'] for r in target.execute(f'PRAGMA table_info({name})')}
               for name in PROJECTION_TABLES}
    for item in projections:
        table = item.get('table')
        row = item.get('row')
        if table not in allowed or not isinstance(row, dict) or set(row) != allowed[table]:
            return False
        columns = sorted(row)
        sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})"
        try:
            target.execute(sql, [row[c] for c in columns])
        except sqlite3.IntegrityError:
            return False
    return True


def _projection_matches(target, version):
    """Check a summarised binding's version coordinates against prefix state."""
    op = version.get('op')
    ident = version.get('id')
    if op == 'objective':
        row = one(target, 'SELECT service,category,revision FROM objectives WHERE id=?', (ident,))
        return bool(row) and (row['service'], row['category'], row['revision']) == \
            (version.get('service'), version.get('category'), version.get('revision'))
    if op == 'shard':
        row = one(target, 'SELECT objective,source,generation FROM request_shards WHERE id=?', (ident,))
        return bool(row) and (row['objective'], row['source'], row['generation']) == \
            (version.get('objective'), version.get('source'), version.get('generation'))
    if op == 'seal':
        return bool(one(target, 'SELECT 1 FROM windows WHERE id=? AND objective=?',
                        (ident, version.get('objective'))))
    if op == 'waiver':
        return bool(one(target, 'SELECT 1 FROM waivers WHERE id=? AND objective=?',
                        (ident, version.get('objective'))))
    if op == 'topology':
        row = one(target, 'SELECT service,generation FROM topology WHERE id=?', (ident,))
        return bool(row) and (row['service'], row['generation']) == \
            (version.get('service'), version.get('generation'))
    if op == 'sample':
        row = one(target, 'SELECT window_id,request_uid,source FROM failure_samples WHERE id=?',
                  (ident,))
        return bool(row) and (row['window_id'], row['request_uid'], row['source']) == \
            (version.get('window'), version.get('request_uid'), version.get('source'))
    if op == 'decide':
        row = one(target, 'SELECT window_id,topology_id FROM cause_cases WHERE id=?', (ident,))
        return bool(row) and (row['window_id'], row['topology_id']) == \
            (version.get('window'), version.get('topology'))
    if op == 'dispute':
        return bool(one(target, 'SELECT 1 FROM cause_disputes WHERE id=? AND case_id=? AND evidence=?',
                        (ident, version.get('case'), version.get('evidence'))))
    if op == 'claim':
        row = one(target, 'SELECT case_id,tenant,period FROM credit_claims WHERE id=?', (ident,))
        return bool(row) and (row['case_id'], row['tenant'], row['period']) == \
            (version.get('case'), version.get('tenant'), version.get('period'))
    if op == 'settle':
        return bool(one(target, 'SELECT 1 FROM settlement_receipts WHERE id=? AND claim_id=?',
                        (ident, version.get('claim'))))
    if op == 'reverse':
        return bool(one(target, 'SELECT 1 FROM settlement_reversals WHERE id=? AND receipt_id=?',
                        (ident, version.get('receipt'))))
    return False


def verify_projected(bundle):
    """Reconstruct the case's dispute scope from anchor, projections, events."""
    required = ('case', 'anchor', 'anchor_seq', 'events', 'projections', 'bindings',
                'retrieval', 'manifest')
    if not all(key in bundle for key in required):
        return None
    if not isinstance(bundle['events'], list) or not isinstance(bundle['projections'], list) \
            or not isinstance(bundle['bindings'], list) or not isinstance(bundle['retrieval'], list):
        return None
    if digest({'anchor': bundle['anchor'], 'bindings': bundle['bindings'],
               'projections': bundle['projections']}) != bundle['manifest']:
        return {'error': 'custody_manifest_conflict'}
    target = connect(':memory:')
    if not _load_projections(target, bundle['projections']):
        target.close()
        return {'error': 'custody_unverifiable'}
    binding_index = {}
    for binding in bundle['bindings']:
        if not isinstance(binding, dict) or set(binding) != {'event_id', 'digest', 'version'}:
            target.close()
            return {'error': 'custody_unverifiable'}
        binding_index[binding['event_id']] = binding
    grant_events = set()
    for grant in bundle['retrieval']:
        if set(grant) != {'id', 'event_id', 'digest', 'version'}:
            target.close()
            return {'error': 'custody_unverifiable'}
        binding = binding_index.get(grant['event_id'])
        if not binding or grant['digest'] != binding['digest'] \
                or grant['version'] != binding['version']:
            target.close()
            return {'error': 'custody_scope_invalid'}
        grant_events.add(grant['event_id'])
    included_ids = set()
    for record in bundle['events']:
        event_id = record.get('payload', {}).get('event_id')
        included_ids.add(event_id)
        binding = binding_index.get(event_id)
        if not binding:
            target.close()
            return {'error': 'custody_evidence_version_conflict'}
        if record_digest(record) != binding['digest'] \
                or version_coords(record) != binding['version']:
            target.close()
            return {'error': 'custody_evidence_version_conflict'}
        try:
            outcome = act(target, record['subsystem'], record['payload'])
        except (KeyError, TypeError, ValueError, sqlite3.IntegrityError):
            target.close()
            return {'error': 'custody_unverifiable'}
        if outcome != record.get('result'):
            target.close()
            return {'error': 'custody_unverifiable'}
    # Summarised originals must still agree with the projected prefix state.
    # A same-UID replacement aimed at another target version carries different
    # coordinates than the projected row the anchor proof actually produced.
    for binding in bundle['bindings']:
        if binding['event_id'] in included_ids:
            continue
        version = binding['version']
        if not _projection_matches(target, version):
            target.close()
            return {'error': 'custody_evidence_version_conflict'}
    # Every retrieval grant must point at a summarised (not included) original.
    if grant_events & included_ids:
        target.close()
        return {'error': 'custody_scope_invalid'}
    case = one(target, 'SELECT * FROM cause_cases WHERE id=?', (bundle['case'],))
    if not case:
        target.close()
        return {'error': 'custody_unverifiable'}
    disputes = many(target, 'SELECT * FROM cause_disputes WHERE case_id=? ORDER BY id',
                    (bundle['case'],))
    result = {'status': case['status'], 'liable': case['liable'],
              'disputes': [dict(row) for row in disputes],
              'summarised': sorted(grant_events),
              'target': target}
    return result


def verify(bundle):
    if not isinstance(bundle, dict) or not isinstance(bundle.get('case'), str):
        return None
    contract = bundle.get('contract', 'full')
    if contract == 'full':
        if not isinstance(bundle.get('events'), list):
            return None
        return verify_full(bundle)
    if contract == 'projected':
        outcome = verify_projected(bundle)
        if not outcome or 'error' in outcome:
            return None
        target = outcome.pop('target')
        target.close()
        return {'status': outcome['status']}
    return None


def _store_side(db, package_id, case_id, bundle):
    db.execute('INSERT OR IGNORE INTO custody_packages VALUES(?,?,?,?)',
               (package_id, case_id, digest(bundle), encode(bundle)))
    covered = []
    if bundle.get('contract') == 'projected':
        covered = [b['event_id'] for b in bundle['bindings']]
    else:
        covered = [r['payload']['event_id'] for r in bundle['events']]
    for event_id in covered:
        binding = next((b for b in bundle.get('bindings', [])
                        if b['event_id'] == event_id), None)
        checksum_value = binding['digest'] if binding else None
        db.execute('INSERT OR IGNORE INTO custody_pins VALUES(?,?,?,?)',
                   (package_id, case_id, event_id, checksum_value))


def _cited_event_ids(db):
    """Events whose action targets an entity still held by a business table.

    Lifecycle actions such as topology upserts reuse the same entity id, so
    citation is decided by the id the payload targets, not by whether each
    event created its own row. Rejected actions (e.g. duplicate samples) never
    created a row and remain genuinely unreferenced.
    """
    live_ids = set()
    for table in ('objectives', 'request_shards', 'windows', 'waivers', 'topology',
                  'failure_samples', 'cause_cases', 'cause_disputes', 'credit_claims',
                  'settlement_receipts', 'settlement_reversals'):
        live_ids.update(r[0] for r in db.execute(f'SELECT id FROM {table}'))
    return {json.loads(row['payload'])['event_id'] for row in _event_rows(db)
            if json.loads(row['payload']).get('id') in live_ids}


def handle(db, command):
    operation = command['op']
    if operation == 'seal':
        if not one(db, 'SELECT id FROM cause_cases WHERE id=?', (command['case'],)):
            return {'decision': 'custody_case_unavailable'}
        if one(db, 'SELECT id FROM custody_packages WHERE id=?', (command['id'],)):
            return {'decision': 'custody_package_exists'}
        contract = command.get('contract', 'full')
        if contract == 'full':
            bundle = package(db, command['case'])
        elif contract == 'projected':
            bundle = projected_package(db, command['case'],
                                       command.get('refs', []), command.get('grants', []),
                                       command.get('project', []))
            if isinstance(bundle, dict) and bundle.get('error'):
                return {'decision': bundle.pop('error'), **bundle}
            if not bundle:
                return {'decision': 'custody_case_unavailable'}
        else:
            return {'decision': 'custody_scope_invalid'}
        bundle['package_id'] = command['id']
        checksum = digest(bundle)
        db.execute('INSERT INTO custody_packages VALUES(?,?,?,?)',
                   (command['id'], command['case'], checksum, encode(bundle)))
        _store_side(db, command['id'], command['case'], bundle)
        return {'decision': 'custody_sealed', 'digest': checksum, 'bundle': bundle,
                'contract': contract,
                'events_included': len(bundle['events']),
                'pinned': [r[0] for r in db.execute(
                    'SELECT event_id FROM custody_pins WHERE package_id=? ORDER BY event_id',
                    (command['id'],))]}
    if operation == 'accept':
        bundle = command['bundle']
        checksum = digest(bundle)
        if checksum != command['digest']:
            return {'decision': 'custody_digest_mismatch'}
        prior = one(db, 'SELECT * FROM custody_receipts WHERE id=?', (command['id'],))
        if prior:
            # Cold storage retries the exact same request: answer idempotently
            # instead of minting a second takeover for the scope.
            if prior['digest'] == checksum:
                return {'decision': 'custody_accepted', 'case_status': prior['decision'],
                        'digest': checksum, 'retry': True}
            return {'decision': 'custody_receipt_exists'}
        contract = bundle.get('contract', 'full')
        if contract == 'full':
            case = verify_full(bundle)
            if not case:
                return {'decision': 'custody_unverifiable'}
            disputes = []
            anchor_trusted = False
        elif contract == 'projected':
            outcome = verify_projected(bundle)
            if not outcome or 'error' in outcome:
                return {'decision': outcome['error'] if outcome else 'custody_unverifiable'}
            target = outcome.pop('target')
            target.close()
            case = {'status': outcome['status']}
            disputes = outcome['disputes']
            anchor_trusted = one(db, 'SELECT anchor FROM custody_trust WHERE anchor=?',
                                 (bundle['anchor'],)) is not None
        else:
            return {'decision': 'custody_unverifiable'}
        package_id = bundle.get('package_id') or command['id']
        for grant in bundle.get('retrieval', []):
            owner = one(db, 'SELECT case_id FROM custody_grants WHERE id=?', (grant['id'],))
            if owner and owner['case_id'] != bundle['case']:
                return {'decision': 'custody_grant_scope_conflict', 'grant': grant['id']}
        db.execute('INSERT INTO custody_receipts VALUES(?,?,?,?)',
                   (command['id'], bundle['case'], checksum, case['status']))
        _store_side(db, package_id, bundle['case'], bundle)
        for grant in bundle.get('retrieval', []):
            db.execute('INSERT OR IGNORE INTO custody_grants VALUES(?,?,?,?,?,?)',
                       (grant['id'], bundle['case'], package_id, grant['event_id'],
                        grant['digest'], encode(grant['version'])))
        return {'decision': 'custody_accepted', 'case_status': case['status'],
                'digest': checksum, 'contract': contract,
                'anchor_trusted': anchor_trusted,
                'disputes': [{'id': d['id'], 'status': d['status'], 'evidence': d['evidence']}
                             for d in disputes]}
    if operation == 'trust':
        db.execute('INSERT OR IGNORE INTO custody_trust VALUES(?,?)',
                   (command['anchor'], command['anchor']))
        return {'decision': 'custody_anchor_trusted', 'anchor': command['anchor']}
    if operation == 'review':
        stored = one(db, 'SELECT * FROM custody_packages WHERE id=?', (command['package'],))
        if not stored:
            return {'decision': 'custody_package_unavailable'}
        bundle = json.loads(stored['payload'])
        if not one(db, 'SELECT id FROM custody_receipts WHERE case_id=? AND digest=?',
                   (bundle['case'], stored['digest'])):
            return {'decision': 'custody_package_unavailable'}
        contract = bundle.get('contract', 'full')
        if contract == 'full':
            target = _replay_records(bundle['events'])
            if target is None:
                return {'decision': 'custody_unverifiable'}
            case = one(target, 'SELECT status FROM cause_cases WHERE id=?', (bundle['case'],))
            disputes = many(target, 'SELECT * FROM cause_disputes WHERE case_id=? ORDER BY id',
                            (bundle['case'],))
            target.close()
            if not case:
                return {'decision': 'custody_unverifiable'}
            return {'decision': 'custody_review_ok', 'contract': 'full',
                    'case_status': case['status'],
                    'disputes': [dict(row) for row in disputes],
                    'requires_retrieval': []}
        outcome = verify_projected(bundle)
        if not outcome or 'error' in outcome:
            return {'decision': outcome['error'] if outcome else 'custody_unverifiable'}
        target = outcome.pop('target')
        target.close()
        return {'decision': 'custody_review_ok', 'contract': 'projected',
                'case_status': outcome['status'], 'disputes': outcome['disputes'],
                'requires_retrieval': outcome['summarised']}
    if operation == 'retrieve':
        stored = one(db, 'SELECT * FROM custody_packages WHERE id=?', (command['package'],))
        if not stored:
            return {'decision': 'custody_package_unavailable'}
        bundle = json.loads(stored['payload'])
        contract = bundle.get('contract', 'full')
        event_id = command['evidence_id']
        included = {r['payload']['event_id']: r for r in bundle['events']}
        if event_id in included:
            return {'decision': 'custody_evidence_served', 'contract': contract,
                    'event_id': event_id, 'payload': included[event_id]['payload']}
        if contract == 'full':
            # Full contract only holds what was copied; nothing else is claimed.
            return {'decision': 'custody_review_restricted', 'event_id': event_id}
        binding = next((b for b in bundle.get('bindings', [])
                        if b['event_id'] == event_id), None)
        grant = next((g for g in bundle.get('retrieval', [])
                      if g['event_id'] == event_id), None)
        if not binding or not grant:
            # No retrieval right for this fact/version: an old summary must not
            # be served as proof of a new fact.
            return {'decision': 'custody_review_restricted', 'event_id': event_id}
        if not command.get('hot_available') or not command.get('evidence'):
            return {'decision': 'custody_review_restricted', 'event_id': event_id,
                    'reason': 'original_not_retrievable'}
        evidence = command['evidence']
        if record_digest(evidence) != binding['digest'] \
                or version_coords(evidence) != binding['version']:
            return {'decision': 'custody_evidence_version_conflict', 'event_id': event_id}
        return {'decision': 'custody_evidence_served', 'contract': 'projected',
                'event_id': event_id, 'payload': evidence['payload']}
    if operation == 'sweep':
        pinned = {r['event_id'] for r in db.execute('SELECT DISTINCT event_id FROM custody_pins')}
        cited = _cited_event_ids(db)
        all_events = [json.loads(row['payload'])['event_id'] for row in _event_rows(db)]
        retained = sorted(pinned | cited)
        releasable = sorted(event_id for event_id in all_events
                            if event_id not in pinned and event_id not in cited)
        return {'decision': 'custody_sweep', 'retained': retained,
                'releasable': releasable}
    return {'decision': 'unknown_operation'}

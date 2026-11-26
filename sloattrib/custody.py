"""Create and verify portable offline SLO accident evidence packages."""
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
'''
SOURCE_MODULES = ('slo', 'cause', 'credit')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def package(db, case_id):
    events = many(db, "SELECT subsystem, payload, result FROM events WHERE subsystem IN ('slo','cause','credit') ORDER BY seq")
    return {'case': case_id, 'events': [{'subsystem': row['subsystem'], 'payload': json.loads(row['payload']),
                                         'result': json.loads(row['result'])} for row in events]}


def verify(bundle):
    if not isinstance(bundle, dict) or not isinstance(bundle.get('events'), list) or not isinstance(bundle.get('case'), str):
        return None
    with closing(connect(':memory:')) as target:
        for record in bundle['events']:
            if not isinstance(record, dict) or record.get('subsystem') not in SOURCE_MODULES:
                return None
            try:
                outcome = act(target, record['subsystem'], record['payload'])
            except (KeyError, TypeError, ValueError, sqlite3.IntegrityError):
                return None
            if outcome != record.get('result'):
                return None
        return one(target, 'SELECT status FROM cause_cases WHERE id=?', (bundle['case'],))


def handle(db, command):
    operation = command['op']
    if operation == 'seal':
        if not one(db, 'SELECT id FROM cause_cases WHERE id=?', (command['case'],)):
            return {'decision': 'custody_case_unavailable'}
        if one(db, 'SELECT id FROM custody_packages WHERE id=?', (command['id'],)):
            return {'decision': 'custody_package_exists'}
        bundle = package(db, command['case'])
        checksum = digest(bundle)
        db.execute('INSERT INTO custody_packages VALUES(?,?,?,?)', (command['id'], command['case'], checksum, encode(bundle)))
        return {'decision': 'custody_sealed', 'digest': checksum, 'bundle': bundle}
    if operation == 'accept':
        bundle = command['bundle']
        checksum = digest(bundle)
        if checksum != command['digest']:
            return {'decision': 'custody_digest_mismatch'}
        if one(db, 'SELECT id FROM custody_receipts WHERE id=?', (command['id'],)):
            return {'decision': 'custody_receipt_exists'}
        case = verify(bundle)
        if not case:
            return {'decision': 'custody_unverifiable'}
        db.execute('INSERT INTO custody_receipts VALUES(?,?,?,?)', (command['id'], bundle['case'], checksum, case['status']))
        return {'decision': 'custody_accepted', 'case_status': case['status'], 'digest': checksum}
    return {'decision': 'unknown_operation'}

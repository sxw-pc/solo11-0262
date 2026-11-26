from .store import one, many

SCHEMA = '''
CREATE TABLE IF NOT EXISTS objectives(id TEXT PRIMARY KEY,service TEXT,category TEXT,revision INTEGER,start_at INTEGER,end_at INTEGER,target_ppm INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS request_shards(id TEXT PRIMARY KEY,objective TEXT,source TEXT,generation INTEGER,requests INTEGER,start_at INTEGER,end_at INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS windows(id TEXT PRIMARY KEY,objective TEXT,expected_sources INTEGER,requests INTEGER,status TEXT,sealed_at INTEGER);
CREATE TABLE IF NOT EXISTS waivers(id TEXT PRIMARY KEY,objective TEXT,start_at INTEGER,end_at INTEGER,security_actor TEXT,owner_actor TEXT,status TEXT);
'''


def handle(db, x):
    op = x['op']
    if op == 'objective':
        if x['end'] <= x['start'] or not 0 <= x['target_ppm'] <= 1000000:
            return {'decision': 'invalid_objective'}
        old = one(db, 'SELECT * FROM objectives WHERE id=?', (x['id'],))
        if old and x['revision'] <= old['revision']:
            return {'decision': 'stale_objective'}
        conflict = one(db, "SELECT id FROM objectives WHERE service=? AND category=? AND id!=? AND status='active' AND start_at<? AND end_at>?", (x['service'], x['category'], x['id'], x['end'], x['start']))
        if conflict:
            return {'decision': 'overlapping_commitment'}
        db.execute("INSERT INTO objectives VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,start_at=excluded.start_at,end_at=excluded.end_at,target_ppm=excluded.target_ppm,status='active'", (x['id'],x['service'],x['category'],x['revision'],x['start'],x['end'],x['target_ppm'],'active'))
        return {'decision': 'objective_active'}
    if op == 'shard':
        objective = one(db, 'SELECT * FROM objectives WHERE id=?', (x['objective'],))
        if not objective or objective['status'] != 'active' or not objective['start_at'] <= x['start'] < x['end'] <= objective['end_at'] or x['requests'] < 0:
            return {'decision': 'shard_outside_contract'}
        old = one(db, 'SELECT * FROM request_shards WHERE id=?', (x['id'],))
        if old:
            return {'decision': 'shard_identity_conflict'}
        if one(db,"SELECT id FROM windows WHERE objective=? AND status='sealed'",(x['objective'],)):
            return {'decision':'window_already_sealed'}
        latest = one(db, 'SELECT MAX(generation) n FROM request_shards WHERE objective=? AND source=?', (x['objective'],x['source']))['n']
        if latest is not None and x['generation'] <= latest:
            return {'decision': 'stale_source_generation'}
        db.execute("UPDATE request_shards SET status='superseded' WHERE objective=? AND source=? AND status='accepted'",(x['objective'],x['source']))
        db.execute('INSERT INTO request_shards VALUES(?,?,?,?,?,?,?,?)', (x['id'],x['objective'],x['source'],x['generation'],x['requests'],x['start'],x['end'],'accepted'))
        return {'decision': 'shard_accepted'}
    if op == 'seal':
        objective = one(db, 'SELECT * FROM objectives WHERE id=?', (x['objective'],))
        if not objective or objective['status'] != 'active' or x['expected_sources'] < 1:
            return {'decision': 'window_denied'}
        shards = many(db, "SELECT * FROM request_shards WHERE objective=? AND status='accepted'", (x['objective'],))
        if len({s['source'] for s in shards}) != x['expected_sources']:
            return {'decision': 'source_gap'}
        db.execute('INSERT INTO windows VALUES(?,?,?,?,?,?)', (x['id'],x['objective'],x['expected_sources'],sum(s['requests'] for s in shards),'sealed',x['at']))
        db.execute("UPDATE request_shards SET status='sealed' WHERE objective=?", (x['objective'],))
        return {'decision': 'window_sealed', 'requests': sum(s['requests'] for s in shards)}
    if op == 'waiver':
        objective = one(db, 'SELECT * FROM objectives WHERE id=?', (x['objective'],))
        if not objective or not objective['start_at'] <= x['start'] < x['end'] <= objective['end_at'] or not x.get('security_actor') or not x.get('owner_actor') or x['security_actor'] == x['owner_actor']:
            return {'decision': 'waiver_denied'}
        db.execute('INSERT INTO waivers VALUES(?,?,?,?,?,?,?)', (x['id'],x['objective'],x['start'],x['end'],x['security_actor'],x['owner_actor'],'active'))
        return {'decision': 'waiver_active'}
    if op == 'retire':
        objective = one(db,'SELECT * FROM objectives WHERE id=?',(x['id'],))
        if not objective or objective['status'] != 'active': return {'decision':'not_active'}
        db.execute("UPDATE objectives SET status='retired' WHERE id=?",(x['id'],))
        db.execute("UPDATE credit_claims SET status='frozen' WHERE case_id IN (SELECT c.id FROM cause_cases c JOIN windows w ON w.id=c.window_id WHERE w.objective=?) AND status='draft'",(x['id'],))
        return {'decision':'retired_and_claims_frozen'}
    return {'decision': 'unknown_operation'}

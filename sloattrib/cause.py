from .store import one, many

SCHEMA = '''
CREATE TABLE IF NOT EXISTS topology(id TEXT PRIMARY KEY,service TEXT,generation INTEGER,dependency TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS failure_samples(id TEXT PRIMARY KEY,window_id TEXT,request_uid TEXT,source TEXT,at INTEGER,kind TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS cause_cases(id TEXT PRIMARY KEY,window_id TEXT,topology_id TEXT,topology_generation INTEGER,status TEXT,liable TEXT,failed_requests INTEGER);
CREATE TABLE IF NOT EXISTS cause_disputes(id TEXT PRIMARY KEY,case_id TEXT,actor TEXT,evidence TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS reconciliations(id TEXT PRIMARY KEY,window_id TEXT,request_uid TEXT,source TEXT,start_at INTEGER,end_at INTEGER,status TEXT);
'''

# Columns added after the first sealed snapshots; applied lazily so older
# databases keep replaying instead of failing to open.
COLUMN_MIGRATIONS = {
    'cause_cases': (
        ('version', 'INTEGER NOT NULL DEFAULT 1'),
        ('failures_lower', 'INTEGER'),
        ('failures_upper', 'INTEGER'),
        ('allowed_failures', 'INTEGER'),
    ),
}


def migrate(db):
    for table, columns in COLUMN_MIGRATIONS.items():
        existing = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
        for name, declaration in columns:
            if name not in existing:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
    db.execute(
        "UPDATE cause_cases SET failures_lower=failed_requests WHERE failures_lower IS NULL")
    db.execute(
        "UPDATE cause_cases SET failures_upper=failed_requests WHERE failures_upper IS NULL")
    db.execute(
        "UPDATE cause_cases SET allowed_failures=("
        "SELECT w.requests*(1000000-o.target_ppm)/1000000 "
        "FROM windows w JOIN objectives o ON o.id=w.objective "
        "WHERE w.id=cause_cases.window_id) WHERE allowed_failures IS NULL")


def _merged_waivers(db, objective_id):
    """Union of active dual-signed waiver intervals, merged into disjoint
    half-open [start, end) segments."""
    rows = many(db, "SELECT start_at,end_at FROM waivers WHERE objective=? AND status='active'",
                (objective_id,))
    intervals = sorted((r['start_at'], r['end_at']) for r in rows)
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _covered(lo, hi, merged):
    """True only when the whole half-open segment [lo, hi) lies inside the
    merged waiver coverage."""
    cursor = lo
    for start, end in merged:
        if end <= cursor:
            continue
        if start > cursor:
            return False
        cursor = end
        if cursor >= hi:
            return True
    return cursor >= hi


def _intersects(lo, hi, merged):
    return any(start < hi and end > lo for start, end in merged)


def _uid_evidence(db, window):
    """Build per-UID evidence segments.

    Without a successful reconciliation endorsement the segment spans the
    min/max timestamps of the trusted (non-client) observations.  A named
    source endorsement narrows the segment to the confirmed sampling range;
    several endorsements intersect.
    """
    samples = many(db, "SELECT * FROM failure_samples WHERE window_id=? AND status='accepted'",
                   (window['id'],))
    by_request = {}
    for sample in samples:
        by_request.setdefault(sample['request_uid'], []).append(sample)
    endorsements = many(db,
        "SELECT * FROM reconciliations WHERE window_id=? AND status='success'", (window['id'],))
    by_endorsement = {}
    for endorsement in endorsements:
        by_endorsement.setdefault(endorsement['request_uid'], []).append(endorsement)
    evidence = {}
    for uid, records in by_request.items():
        trusted = [r for r in records if r['kind'] != 'client']
        if not trusted:
            continue
        lo = min(r['at'] for r in trusted)
        hi = max(r['at'] for r in trusted) + 1
        notes = by_endorsement.get(uid)
        if notes:
            elo = max(n['start_at'] for n in notes)
            ehi = min(n['end_at'] for n in notes)
            # Endorsements that do not share a common range cannot narrow the
            # evidence; fall back to the raw observation bounds.
            if elo >= ehi:
                elo, ehi = lo, hi
        else:
            elo, ehi = lo, hi
        evidence[uid] = {
            'lo': lo, 'hi': hi, 'elo': elo, 'ehi': ehi,
            'dependency': any(r['kind'] == 'dependency' for r in trusted),
        }
    return evidence


def _evaluate(db, window, objective):
    evidence = _uid_evidence(db, window)
    waivers = _merged_waivers(db, objective['id'])
    supported = set()
    certain = set()
    for uid, item in evidence.items():
        if not _covered(item['elo'], item['ehi'], waivers):
            supported.add(uid)
            # Narrowing can only move the segment inside [lo,hi); when no
            # waiver touches that raw segment, the failure is provable.
            if not _intersects(item['lo'], item['hi'], waivers):
                certain.add(uid)
    return evidence, waivers, supported, certain


def _allowed(window, objective):
    return window['requests'] * (1000000 - objective['target_ppm']) // 1000000


def _verify_claim_lines(db, window, evidence, waivers):
    """Re-check every reserved draft line against current evidence.  Only
    lines whose UID lost business support are frozen; independent UIDs keep
    their reservation."""
    claims = many(db,
        "SELECT c.* FROM credit_claims c JOIN cause_cases k ON k.id=c.case_id "
        "WHERE k.window_id=? AND c.status='draft'", (window['id'],))
    for claim in claims:
        lines = many(db, "SELECT * FROM credit_claim_lines WHERE claim_id=?", (claim['id'],))
        if not lines:
            continue
        all_frozen = True
        for line in lines:
            item = evidence.get(line['request_uid'])
            still_supported = bool(item) and not _covered(item['elo'], item['ehi'], waivers)
            if not still_supported:
                db.execute("UPDATE credit_claim_lines SET status='frozen' WHERE id=?", (line['id'],))
            else:
                all_frozen = False
        if all_frozen:
            db.execute("UPDATE credit_claims SET status='frozen' WHERE id=?", (claim['id'],))


def handle(db,x):
    op=x['op']
    if op=='topology':
        old=one(db,'SELECT * FROM topology WHERE id=?',(x['id'],))
        if old and x['generation']<=old['generation']: return {'decision':'stale_topology'}
        db.execute('INSERT INTO topology VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET generation=excluded.generation,dependency=excluded.dependency,status=excluded.status',(x['id'],x['service'],x['generation'],x['dependency'],'active'))
        db.execute("UPDATE cause_cases SET status='stale' WHERE topology_id=? AND topology_generation<? AND status='decided'",(x['id'],x['generation']))
        db.execute("UPDATE credit_claims SET status='frozen' WHERE case_id IN (SELECT id FROM cause_cases WHERE topology_id=? AND status='stale') AND status='draft'",(x['id'],))
        return {'decision':'topology_active'}
    if op=='sample':
        window=one(db,'SELECT * FROM windows WHERE id=?',(x['window'],))
        objective=one(db,'SELECT * FROM objectives WHERE id=?',(window['objective'],)) if window else None
        if not window or window['status']!='sealed' or not objective or not objective['start_at']<=x['at']<objective['end_at'] or x['kind'] not in ('service','dependency','client'):
            return {'decision':'sample_unavailable'}
        duplicate=one(db,'SELECT id FROM failure_samples WHERE window_id=? AND request_uid=? AND source=?',(x['window'],x['request_uid'],x['source']))
        if duplicate:return {'decision':'duplicate_source_sample'}
        db.execute('INSERT INTO failure_samples VALUES(?,?,?,?,?,?,?)',(x['id'],x['window'],x['request_uid'],x['source'],x['at'],x['kind'],'accepted'))
        return {'decision':'sample_recorded'}
    if op=='reconcile':
        window=one(db,'SELECT * FROM windows WHERE id=?',(x['window'],))
        objective=one(db,'SELECT * FROM objectives WHERE id=?',(window['objective'],)) if window else None
        if not window or window['status']!='sealed' or not objective:
            return {'decision':'reconciliation_denied'}
        records=many(db,"SELECT * FROM failure_samples WHERE window_id=? AND request_uid=? AND status='accepted' AND kind!='client'",(x['window'],x['request_uid']))
        if not records or not any(r['source']==x['source'] for r in records):
            return {'decision':'reconciliation_denied'}
        lo=min(r['at'] for r in records)
        hi=max(r['at'] for r in records)+1
        if x['start']>=x['end'] or x['start']<lo or x['end']>hi:
            return {'decision':'reconciliation_denied'}
        others=many(db,"SELECT * FROM reconciliations WHERE window_id=? AND request_uid=? AND source!=? AND status='success'",(x['window'],x['request_uid'],x['source']))
        if any(max(x['start'],o['start_at'])>=min(x['end'],o['end_at']) for o in others):
            return {'decision':'endorsement_conflict'}
        db.execute("UPDATE reconciliations SET status='superseded' WHERE window_id=? AND request_uid=? AND source=? AND status='success'",(x['window'],x['request_uid'],x['source']))
        db.execute('INSERT INTO reconciliations VALUES(?,?,?,?,?,?,?)',(x['id'],x['window'],x['request_uid'],x['source'],x['start'],x['end'],'success'))
        return {'decision':'reconciliation_recorded'}
    if op=='seal_case':
        case=one(db,'SELECT * FROM cause_cases WHERE id=?',(x['case'],))
        if not case or case['status'] not in ('decided','within_target'):
            return {'decision':'seal_denied'}
        db.execute("UPDATE cause_cases SET status='sealed' WHERE id=?",(x['case'],))
        return {'decision':'case_sealed'}
    if op=='decide':
        window=one(db,'SELECT * FROM windows WHERE id=?',(x['window'],))
        objective=one(db,'SELECT * FROM objectives WHERE id=?',(window['objective'],)) if window else None
        topo=one(db,'SELECT * FROM topology WHERE id=?',(x['topology'],))
        if not window or not objective or not topo or topo['service']!=objective['service']:
            return {'decision':'source_unavailable'}
        if window['requests']==0:return {'decision':'unknown_denominator'}
        if one(db,'SELECT id FROM cause_cases WHERE id=?',(x['id'],)):
            return {'decision':'case_identity_conflict'}
        samples=many(db,"SELECT * FROM failure_samples WHERE window_id=? AND status='accepted'",(x['window'],))
        allowed=_allowed(window,objective)
        if not samples:
            db.execute("UPDATE cause_cases SET status='superseded' WHERE window_id=? AND status IN ('pending_evidence','awaiting_evidence')",(x['window'],))
            version=one(db,'SELECT COALESCE(MAX(version),0)+1 v FROM cause_cases WHERE window_id=?',(x['window'],))['v']
            db.execute('INSERT INTO cause_cases VALUES(?,?,?,?,?,?,?,?,?,?,?)',(x['id'],x['window'],topo['id'],topo['generation'],'pending_evidence','unknown',0,version,0,0,allowed))
            return {'decision':'pending_evidence','liable':'unknown','failed_requests':0,'failures_lower':0,'failures_upper':0,'allowed_failures':allowed,'version':version}
        evidence,waivers,supported,certain=_evaluate(db,window,objective)
        if not evidence:
            lower=upper=0
            liable='service'
        else:
            lower=len(certain)
            upper=len(supported)
            if upper==0:
                liable='waived'
            elif any(item['dependency'] for uid,item in evidence.items() if uid in supported) and topo['dependency']:
                liable='dependency'
            else:
                liable='service'
        if lower>allowed:
            status='decided'
        elif upper<=allowed:
            status='within_target'
        else:
            status='awaiting_evidence'
        db.execute("UPDATE cause_cases SET status='sealed' WHERE window_id=? AND status IN ('decided','within_target')",(x['window'],))
        db.execute("UPDATE cause_cases SET status='superseded' WHERE window_id=? AND status IN ('pending_evidence','awaiting_evidence')",(x['window'],))
        version=one(db,'SELECT COALESCE(MAX(version),0)+1 v FROM cause_cases WHERE window_id=?',(x['window'],))['v']
        db.execute('INSERT INTO cause_cases VALUES(?,?,?,?,?,?,?,?,?,?,?)',(x['id'],x['window'],topo['id'],topo['generation'],status,liable,lower,version,lower,upper,allowed))
        _verify_claim_lines(db,window,evidence,waivers)
        return {'decision':status,'liable':liable,'failed_requests':lower,'failures_lower':lower,'failures_upper':upper,'allowed_failures':allowed,'version':version}
    if op=='dispute':
        case=one(db,'SELECT * FROM cause_cases WHERE id=?',(x['case'],))
        if not case or case['status']!='decided' or not x.get('evidence'):return {'decision':'dispute_denied'}
        db.execute('INSERT INTO cause_disputes VALUES(?,?,?,?,?)',(x['id'],x['case'],x['actor'],x['evidence'],'open'))
        db.execute("UPDATE cause_cases SET status='disputed' WHERE id=?",(x['case'],))
        db.execute("UPDATE credit_claims SET status='frozen' WHERE case_id=? AND status='draft'",(x['case'],))
        return {'decision':'dispute_open'}
    if op=='resolve':
        dispute=one(db,'SELECT * FROM cause_disputes WHERE id=?',(x['id'],))
        if not dispute or dispute['status']!='open' or x['liable'] not in ('service','dependency','waived') or not x.get('reviewer'):
            return {'decision':'resolution_denied'}
        db.execute("UPDATE cause_disputes SET status='resolved' WHERE id=?",(x['id'],))
        db.execute("UPDATE cause_cases SET status='decided',liable=? WHERE id=?",(x['liable'],dispute['case_id']))
        return {'decision':'cause_resolved','liable':x['liable']}
    return {'decision':'unknown_operation'}

from .store import one, many

SCHEMA = '''
CREATE TABLE IF NOT EXISTS topology(id TEXT PRIMARY KEY,service TEXT,generation INTEGER,dependency TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS failure_samples(id TEXT PRIMARY KEY,window_id TEXT,request_uid TEXT,source TEXT,at INTEGER,kind TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS cause_cases(id TEXT PRIMARY KEY,window_id TEXT,topology_id TEXT,topology_generation INTEGER,status TEXT,liable TEXT,failed_requests INTEGER);
CREATE TABLE IF NOT EXISTS cause_disputes(id TEXT PRIMARY KEY,case_id TEXT,actor TEXT,evidence TEXT,status TEXT);
'''


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
    if op=='decide':
        window=one(db,'SELECT * FROM windows WHERE id=?',(x['window'],))
        objective=one(db,'SELECT * FROM objectives WHERE id=?',(window['objective'],)) if window else None
        topo=one(db,'SELECT * FROM topology WHERE id=?',(x['topology'],))
        if not window or not objective or not topo or topo['service']!=objective['service']:
            return {'decision':'source_unavailable'}
        samples=many(db,"SELECT * FROM failure_samples WHERE window_id=? AND status='accepted'",(x['window'],))
        if not samples:
            db.execute('INSERT INTO cause_cases VALUES(?,?,?,?,?,?,?)',(x['id'],x['window'],topo['id'],topo['generation'],'pending_evidence','unknown',0))
            return {'decision':'pending_evidence'}
        by_request={}
        for sample in samples: by_request.setdefault(sample['request_uid'],[]).append(sample)
        waiver=one(db,'SELECT 1 FROM waivers WHERE objective=? AND status=? AND start_at<=? AND end_at>? LIMIT 1',(objective['id'],'active',x['at'],x['at']))
        bad={uid for uid,records in by_request.items() if any(r['kind']!='client' for r in records)}
        if window['requests']==0:return {'decision':'unknown_denominator'}
        failures=0 if waiver else len(bad)
        liable='waived' if waiver else ('dependency' if any(r['kind']=='dependency' for r in samples) and topo['dependency'] else 'service')
        allowed=window['requests']*(1000000-objective['target_ppm'])//1000000
        status='decided' if failures>allowed else 'within_target'
        db.execute('INSERT INTO cause_cases VALUES(?,?,?,?,?,?,?)',(x['id'],x['window'],topo['id'],topo['generation'],status,liable,failures))
        return {'decision':status,'liable':liable,'failed_requests':failures}
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

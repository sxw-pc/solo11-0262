import argparse
import json
from pathlib import Path
from .settings import MODULES, DEMO
from .store import act, connect, replay, many


def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=[*MODULES, 'demo', 'replay', 'report'])
    p.add_argument('--db', default='state.sqlite')
    p.add_argument('--fixture')
    args = p.parse_args()
    if args.command == 'replay':
        result = replay(args.db)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result['verified']:
            raise SystemExit(2)
        return
    db = connect(args.db)
    try:
        if args.command == 'report':
            result = many(db, 'SELECT * FROM events ORDER BY seq')
        else:
            files = DEMO if args.command == 'demo' else [(args.command, args.fixture)]
            if any(not file for _, file in files):
                p.error('--fixture required')
            result = []
            for subsystem, file in files:
                steps = json.loads(Path(file).read_text(encoding='utf-8'))
                results = []
                for step in steps:
                    # A {'subsystem': ..., 'command': ...} envelope lets one
                    # fixture drive a re-entrant handover across modules.
                    if isinstance(step, dict) and {'subsystem', 'command'} <= set(step):
                        target, payload = step['subsystem'], {**step['command'], 'event_id': step['event_id']}
                        outcome = act(db, target, payload)
                        results.append({'subsystem': target, 'result': outcome})
                    else:
                        results.append(act(db, subsystem, step))
                result.append({'fixture': file, 'results': results})
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == '__main__':
    main()

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
                routed = []
                for step in steps:
                    # FX fixtures interleave credit claims with FX commands; route by op.
                    target = 'credit' if subsystem == 'fx' and step.get('op') == 'claim' else subsystem
                    routed.append(act(db, target, step))
                result.append({'fixture': file, 'results': routed})
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == '__main__':
    main()

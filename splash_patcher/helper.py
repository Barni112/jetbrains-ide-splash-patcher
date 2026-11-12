"""Install/restore entry point; may run as a short-lived elevated process."""
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from splash_patcher.transactions import execute, write_json


def main():
    plan = Path(sys.argv[1]).resolve(strict=True)
    result = plan.with_suffix('.result.json')
    try:
        execute(plan, recover='--recover' in sys.argv[2:])
    except Exception as exc:
        write_json(result, {'ok': False, 'error': str(exc)})
        return 1
    write_json(result, {'ok': True})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import cast

from .dependencies import RECIPES, load_projects
from .models import Plan, PlatformPolicy
from .modes import DEFAULT_MODE, MODES, parse_mode

ROOT = Path(__file__).resolve().parents[1]


def load_platforms() -> PlatformPolicy:
    return cast(PlatformPolicy, json.loads((ROOT / 'data/platforms.json').read_text()))


def plan(commit: str, platform: str, mode: str) -> Plan:
    parse_mode(mode)
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('CPython commit must be a full lowercase 40-character SHA')
    platforms = load_platforms()
    if platform not in platforms['platforms']:
        raise ValueError('platform must be one of the recorded PEP 11 Tier 1 targets')
    return dict(cpython_commit=commit, platform=platform, mode=mode,
                platform_policy=platforms, recipes_sha256=hashlib.sha256(RECIPES.read_bytes()).hexdigest(),
                status='qualification-required', jobs=[
                    dict(project=p['name'], release=p['release'],
                         status='not-applicable' if p['name'] == 'uvloop' and 'windows' in platform
                         else 'qualification-required')
                    for p in load_projects()])


def main() -> int:
    parser = argparse.ArgumentParser(description='Plan and run downstream CPython tests')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list', help='Show projects and versions from recipes.toml')
    planning = sub.add_parser('plan', help='Resolve GitHub refs and plan a CPython comparison')
    reference = planning.add_mutually_exclusive_group(required=True)
    reference.add_argument('--ref', help='Branch, commit, PR URL, or owner/repo:branch')
    reference.add_argument('--commit', help='Produce a low-level single-commit plan instead')
    planning.add_argument('--base-ref', help='Baseline ref (default: PR base branch, otherwise main)')
    planning.add_argument('--platform', required=True, choices=tuple(load_platforms()['platforms']))
    planning.add_argument('--mode', choices=MODES, default=DEFAULT_MODE)
    worker = sub.add_parser('worker', help='Execute a prepared job inside an already isolated disposable worker')
    worker.add_argument('job', type=Path)
    worker.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'list':
            for p in load_projects():
                print(f"{p['name']:16} {p['release']}")
        elif args.command == 'plan':
            if args.ref is not None:
                from .comparison import make_plan
                print(json.dumps(make_plan(args.ref, [args.platform], args.mode, base_ref=args.base_ref), indent=2))
            else:
                if args.base_ref is not None:
                    parser.error('--base-ref requires --ref')
                print(json.dumps(plan(args.commit, args.platform, args.mode), indent=2))
        else:
            from .worker import run_job
            result = run_job(args.job, args.output)
            print(json.dumps(result, indent=2))
            return 0 if result['status'] == 'passed' else 1
    except (ValueError, OSError, KeyError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

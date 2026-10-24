import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import cast

from .dependencies import RECIPES, load_projects, load_recipes
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
    recipes = load_recipes()
    return dict(cpython_commit=commit, platform=platform, mode=mode,
                platform_policy=platforms, recipes_sha256=hashlib.sha256(RECIPES.read_bytes()).hexdigest(),
                status='qualification-required', jobs=[
                    dict(project=p['name'], release=p['release'],
                         status='not-applicable' if platform in recipes[p['name']].get('exclude_platforms', [])
                         else 'qualification-required')
                    for p in load_projects()])


def main() -> int:
    parser = argparse.ArgumentParser(description='Plan and run downstream CPython tests')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list', help='Show projects and versions from recipes.toml')
    updating = sub.add_parser('update', help='Update one project, or all projects, from PyPI')
    updating.add_argument('package', nargs='?', help='Package name (default: all configured projects)')
    updating.add_argument('--version', help='Select a particular release (requires a package)')
    updating.add_argument('--tag', help='Override the release tag template (requires a package)')
    adding = sub.add_parser('add', help='Add a PyPI project and its upstream test recipe')
    adding.add_argument('package')
    adding.add_argument('--version', help='Select a particular release (default: latest on PyPI)')
    adding.add_argument('--source', choices=('git', 'sdist'), default='git', help='Location of tests (default: git)')
    adding.add_argument('--repository', default='', help='HTTPS Git URL (required for Git test sources)')
    adding.add_argument('--tag', default='', help='Release tag template, e.g. v{version} (required for Git sources)')
    adding.add_argument('--commit', help='Optional expected full SHA; must match the resolved release tag')
    adding.add_argument('--module', required=True, help='Python import name, e.g. PIL')
    adding.add_argument('--tests', default='tests', help='Relative test path (default: tests)')
    adding.add_argument('--installed', action='store_true', help='Run tests from the installed package')
    adding.add_argument('--adapter', choices=('django', 'lxml', 'cython'))
    adding.add_argument('--exclude-platform', dest='exclude_platforms', action='append',
                        choices=tuple(load_platforms()['platforms']), help='Unsupported target triple (repeatable)')
    adding.add_argument('--import-mode', choices=('importlib', 'prepend', 'append'))
    requirements = adding.add_mutually_exclusive_group()
    requirements.add_argument('--dependency', dest='dependencies', action='append',
                              help='Test requirement (repeatable; default: pytest)')
    requirements.add_argument('--no-test-dependencies', action='store_true', help='No additional test requirements')
    for flag, destination, description in (
        ('build-dependency', 'build_dependencies', 'Build requirement (repeatable)'),
        ('linux', 'linux', 'apt package (repeatable)'),
        ('macos', 'macos', 'Homebrew formula (repeatable)'),
        ('windows', 'windows', 'vcpkg package (repeatable)'),
        ('selection', 'selection', 'Django test selection (repeatable)'),
        ('pytest-arg', 'pytest_args', 'Pytest argument (repeatable; use --pytest-arg=-W for flags)'),
        ('test-env', 'test_env', 'Test environment NAME=VALUE (repeatable)'),
    ):
        adding.add_argument('--' + flag, dest=destination, action='append', help=description)
    for managing in (updating, adding):
        managing.add_argument('--recipes', type=Path, default=RECIPES, help='Recipe file (default: data/recipes.toml)')
        managing.add_argument('--dry-run', action='store_true', help='Show the diff without changing the file')
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
        elif args.command == 'update':
            from .projects import update_projects
            diff = update_projects(args.package, version=args.version, tag=args.tag,
                                   path=args.recipes, dry_run=args.dry_run)
            if diff:
                print(diff, end='')
            else:
                print('No project changes.')
        elif args.command == 'add':
            from .projects import LIST_FIELDS, add_project
            configuration: dict[str, object] = dict(repository=args.repository, tag=args.tag, source=args.source,
                                                    module=args.module, tests=args.tests,
                                                    dependencies=[] if args.no_test_dependencies else
                                                    args.dependencies if args.dependencies is not None else ['pytest'])
            for field in LIST_FIELDS:
                if getattr(args, field) is not None:
                    configuration[field] = getattr(args, field)
            for field in ('adapter', 'import_mode'):
                if getattr(args, field) is not None:
                    configuration[field] = getattr(args, field)
            if args.installed:
                configuration['installed'] = True
            if args.test_env is not None:
                test_env: dict[str, str] = {}
                for item in args.test_env:
                    key, separator, value = item.partition('=')
                    if not separator or key in test_env:
                        raise ValueError('--test-env requires distinct NAME=VALUE entries')
                    test_env[key] = value
                configuration['test_env'] = test_env
            diff = add_project(args.package, configuration, version=args.version, commit=args.commit,
                               path=args.recipes, dry_run=args.dry_run)
            print(diff, end='')
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

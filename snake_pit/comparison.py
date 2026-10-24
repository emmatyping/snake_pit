"""Plan paired CPython runs, prune build blockers, and report observed differences."""
import argparse
from datetime import datetime, timezone
import hashlib
from html import escape
import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET
from typing import TypedDict, cast

from .__main__ import load_platforms
from .actions import Target, matrices
from .artifacts import locked_versions, normalize, read_results
from .dependencies import RECIPES, load_projects
from .models import ProjectResult
from .modes import DEFAULT_MODE, MODES
from .refs import Revision, parse_ref, resolve, resolve_pull


class ComparisonPlan(TypedDict):
    baseline: Revision
    target: Revision
    mode: str
    platforms: list[Target]
    projects: list[str]
    recipes_sha256: str
    same_commit: bool


class TestOutcome(TypedDict):
    outcome: str
    message: str


class Difference(TypedDict):
    project: str
    platform: str
    mode: str
    change: str
    baseline: str
    target: str
    new_failures: list[str]
    fixed_failures: list[str]
    newly_observed_failures: list[str]
    changed_failures: list[str]
    dependency_changes: dict[str, list[str | None]]
    blocked_by: list[str]
    lost_tests: list[str]
    test_counts: dict[str, int]
    notes: list[str]


def make_plan(ref: str, platforms: list[str], mode: str, projects: list[str] | None = None,
              *, base_ref: str | None = None) -> ComparisonPlan:
    catalog = [p['name'] for p in load_projects()]
    if projects is not None and (not projects or len(projects) != len(set(projects)) or set(projects) - set(catalog)):
        raise ValueError('provide distinct project names from recipes.toml')
    if base_ref is None and parse_ref(ref)[2] is not None:
        target, baseline = resolve_pull(ref)
    else:
        baseline_ref = 'main' if base_ref is None else base_ref
        baseline = resolve(baseline_ref)
        target = baseline.copy() if parse_ref(ref) == parse_ref(baseline_ref) else resolve(ref)
        target['ref'] = ref
    value = matrices(baseline['commit'], platforms, mode)
    return dict(baseline=baseline, target=target, mode=mode, platforms=value['builds'],
                projects=catalog if projects is None else projects,
                recipes_sha256=hashlib.sha256(RECIPES.read_bytes()).hexdigest(),
                same_commit=baseline['commit'] == target['commit'])


def emit_plan(value: ComparisonPlan, path: Path) -> None:
    outputs = {'baseline': json.dumps(value['baseline']), 'target': json.dumps(value['target']),
               'builds': json.dumps({'include': value['platforms']}), 'projects': json.dumps(value['projects']),
               'same_commit': str(value['same_commit']).lower(),
               'revisions': json.dumps([dict(side='baseline', **value['baseline'])] +
                                       ([] if value['same_commit'] else [dict(side='target', **value['target'])]))}
    with path.open('a') as stream:
        for name, output in outputs.items():
            stream.write(f'{name}={output}\n')


def placeholder(project: str, release: str, commit: str, platform: str, mode: str, reason: str) -> ProjectResult:
    return dict(project=project, release=release, cpython_commit=commit, platform=platform, mode=mode,
                status='infrastructure-error', failed_phase='orchestration', error=reason, phases=[], artifacts={},
                build_status='not-run', started_at=datetime.now(timezone.utc).isoformat())


def schedule(reports: Path, output: Path, commit: str, platform: str, mode: str,
             projects: list[str] | None = None) -> list[str]:
    """Only verified build unavailability blocks a matching locked dependency."""
    results = read_results(reports, commit, platform, mode)
    failed = {normalize(name): result['release'] for name, (_, result) in results.items()
              if result.get('build_status') == 'failed'}
    eligible: list[str] = []
    for project in load_projects():
        name = project['name']
        if projects is not None and name not in projects:
            continue
        if name not in results:
            result = placeholder(name, project['release'], commit, platform, mode, 'package preparation report is missing')
        else:
            folder, original = results[name]
            result = original.copy()
            if result.get('recipe_sha256') != hashlib.sha256(RECIPES.read_bytes()).hexdigest():
                raise ValueError(f'preparation recipe mismatch: {name}')
            dependencies: set[tuple[str, str]] = set()
            for lock in sorted(folder.glob('pylock*.toml')):
                dependencies.update(locked_versions(lock).items())
            blockers = sorted(f'{dep}=={version}' for dep, version in dependencies
                              if dep != normalize(name) and failed.get(dep) == version)
            if blockers:
                result['status'] = 'blocked'
                result['failed_phase'] = 'dependencies'
                result['blocked_by'] = blockers
            elif result['status'] == 'built' and result.get('build_status') == 'passed':
                eligible.append(name)
                continue
        destination = output / name
        destination.mkdir(parents=True, exist_ok=True)
        (destination / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        if name in results:
            # Keep the locks which explain the blocker in the summary artifact.
            for lock in results[name][0].glob('pylock*.toml'):
                shutil.copyfile(lock, destination / lock.name)
    return eligible


def junit(path: Path) -> dict[str, TestOutcome]:
    outcomes: dict[str, TestOutcome] = {}
    if not path.exists():
        return outcomes
    messages: dict[str, set[str]] = {}
    severity = {'passed': 0, 'skipped': 1, 'failed': 2}
    for case in ET.parse(path).iter('testcase'):
        key = f'{case.get("classname", "")}::{case.get("name", "")}'
        # Paths vary between runner operating systems and working directories;
        # class/name IDs remain stable within a paired platform run.
        failures = [*case.findall('failure'), *case.findall('error')]
        outcome = 'failed' if failures else 'skipped' if case.find('skipped') is not None else 'passed'
        # Pytest can emit separate call and teardown records for the same ID.
        # Some custom collectors also reuse IDs. Compare the group conservatively:
        # every record must pass to call it passing, and retain every failure.
        previous = outcomes.get(key)
        if previous is not None and severity[previous['outcome']] > severity[outcome]:
            outcome = previous['outcome']
        messages.setdefault(key, set()).update(failure.get('message', '') for failure in failures)
        outcomes[key] = dict(outcome=outcome, message='\n'.join(sorted(messages[key])))
    return outcomes


def description(result: ProjectResult) -> str:
    phase = result.get('failed_phase')
    return result['status'] + (f' ({phase})' if phase else '')


def difference(before: ProjectResult, after: ProjectResult, old_tests: dict[str, TestOutcome],
               new_tests: dict[str, TestOutcome]) -> Difference:
    old_outcomes = {k: v['outcome'] for k, v in old_tests.items()}
    new_outcomes = {k: v['outcome'] for k, v in new_tests.items()}
    new = sorted(k for k, v in new_tests.items() if v['outcome'] == 'failed'
                 and old_outcomes.get(k) == 'passed')
    fixed = sorted(k for k, v in old_tests.items() if v['outcome'] == 'failed'
                   and new_outcomes.get(k) == 'passed')
    observed = sorted(k for k, v in new_tests.items() if v['outcome'] == 'failed'
                      and old_outcomes.get(k) not in ('passed', 'failed'))
    changed = sorted(k for k, v in new_tests.items() if v['outcome'] == 'failed'
                     and old_outcomes.get(k) == 'failed'
                     and v['message'] != old_tests[k]['message'])
    lost = sorted(k for k, v in old_tests.items() if v['outcome'] == 'passed'
                  and new_outcomes.get(k) not in ('passed', 'failed'))
    old_deps, new_deps = before.get('dependencies', {}), after.get('dependencies', {})
    drift = {name: [old_deps.get(name), new_deps.get(name)] for name in sorted(old_deps.keys() | new_deps.keys())
             if old_deps.get(name) != new_deps.get(name)}
    status, previous = after['status'], before['status']
    infrastructure_phases = {'orchestration', 'restore', 'prepared-artifact', 'system-dependencies',
                             'compiler-environment', 'native-dependencies', 'source-download',
                             'test-source', 'test-source-identity', 'archive-test-source', 'venv', 'rust-target'}
    uncertain = any(r['status'] in ('infrastructure-error', 'running', 'built', 'timeout') or
                    r['status'] != 'passed' and r.get('failed_phase') in infrastructure_phases
                    for r in (before, after))
    if uncertain:
        change = 'inconclusive'
    elif status == 'blocked':
        change = 'unchanged-blocked' if previous == 'blocked' and before.get('blocked_by') == after.get('blocked_by') else 'newly-blocked'
    elif status == 'not-applicable' and previous == status:
        change = 'not-applicable'
    elif status == 'passed' and previous != 'passed':
        change = 'improved'
    elif previous == 'passed' and status != 'passed' or new:
        change = 'regression'
    elif before.get('build_status') == 'passed' and after.get('build_status') == 'failed':
        change = 'regression'
    elif lost:
        change = 'coverage-change'
    elif previous == status == 'passed':
        change = 'unchanged-pass'
    elif fixed or before.get('build_status') == 'failed' and after.get('build_status') == 'passed':
        change = 'improved'
    elif observed:
        change = 'newly-observed-failure'
    elif changed or description(before) != description(after):
        change = 'changed-failure'
    else:
        change = 'unchanged-failure'
    # A failure before resolution has no complete lock. Compare the versions
    # that both sides actually resolved; retain all differences for inspection.
    if any(name in old_deps and name in new_deps for name in drift) and change in ('regression', 'improved'):
        change = 'inconclusive'
    return dict(project=after['project'], platform=after['platform'], mode=after['mode'], change=change,
                baseline=description(before), target=description(after), new_failures=new, fixed_failures=fixed,
                newly_observed_failures=observed, changed_failures=changed, dependency_changes=drift,
                blocked_by=after.get('blocked_by', []), lost_tests=lost,
                test_counts={'baseline': len(old_tests), 'target': len(new_tests)},
                notes=[r['error'] for r in (before, after) if r.get('error')])


def report(plan: ComparisonPlan, artifacts: Path) -> dict[str, object]:
    rows: list[Difference] = []
    complete = True
    for target in plan['platforms']:
        platform, mode = target['platform'], plan['mode']
        side_results: dict[str, dict[str, tuple[Path, ProjectResult]]] = {}
        interpreters: dict[str, ProjectResult] = {}
        for side in ('baseline', 'target'):
            revision = plan[side] if side == 'baseline' else plan['target']
            found: dict[str, tuple[Path, ProjectResult]] = {}
            for path in sorted(artifacts.rglob('result.json')):
                data = cast(ProjectResult, json.loads(path.read_text()))
                if (data.get('cpython_commit'), data.get('platform'), data.get('mode')) != (revision['commit'], platform, mode):
                    continue
                if data.get('recipe_sha256') not in (None, plan['recipes_sha256']):
                    raise ValueError('comparison artifacts use different recipes')
                name = data['project']
                if name in found:
                    raise ValueError(f'duplicate outcome for {side}/{platform}/{name}')
                found[name] = (path.parent, data)
            side_results[side] = found
            interpreter = placeholder('CPython', '', revision['commit'], platform, mode, 'interpreter report is missing')
            for path in artifacts.rglob('interpreter.json'):
                item = cast(ProjectResult, json.loads(path.read_text()))
                if (item.get('cpython_commit'), item.get('platform'), item.get('mode')) == (revision['commit'], platform, mode) and item.get('error') != 'missing':
                    interpreter = item
                    break
            interpreters[side] = interpreter
        rows.append(difference(interpreters['baseline'], interpreters['target'], {}, {}))
        if any(interpreters[s]['status'] != 'passed' for s in interpreters):
            complete = False
            continue  # One interpreter build result, rather than invented package failures.
        for name in plan['projects']:
            pair: dict[str, tuple[Path, ProjectResult]] = {}
            for side in ('baseline', 'target'):
                revision = plan['baseline'] if side == 'baseline' else plan['target']
                pair[side] = side_results[side].get(name, (artifacts, placeholder(
                    name, '', revision['commit'], platform, mode, 'project outcome is missing')))
            tests: dict[str, dict[str, TestOutcome]] = {}
            for side in ('baseline', 'target'):
                try:
                    tests[side] = junit(pair[side][0] / 'tests.xml')
                except (ET.ParseError, ValueError, OSError) as exc:
                    tests[side] = {}
                    original = pair[side][1].copy()
                    original['status'] = 'infrastructure-error'
                    original['error'] = f'invalid JUnit results: {exc}'
                    pair[side] = (pair[side][0], original)
            rows.append(difference(pair['baseline'][1], pair['target'][1], tests['baseline'], tests['target']))
    counts: dict[str, int] = {}
    for row in rows:
        counts[row['change']] = counts.get(row['change'], 0) + 1
    return {'baseline': plan['baseline'], 'target': plan['target'], 'mode': plan['mode'],
            'recipes_sha256': plan['recipes_sha256'], 'counts': counts, 'differences': rows, 'complete': complete}


def markdown(value: dict[str, object]) -> str:
    baseline, target = cast(Revision, value['baseline']), cast(Revision, value['target'])
    rows = cast(list[Difference], value['differences'])
    counts = cast(dict[str, int], value['counts'])
    lines = ['# CPython downstream comparison', '',
             f'Baseline: `{baseline["repository"]}@{baseline["commit"]}`',
             f'Target: `{target["repository"]}@{target["commit"]}`', '',
             ', '.join(f'{count} {change}' for change, count in sorted(counts.items())), '',
             'Dependency builds gate consumers; dependency test failures do not. '
             'A changed result is evidence for investigation, not proof of a CPython bug.', '']
    if not value['complete']:
        lines.extend(['Interpreter preparation was incomplete; package comparisons could not run on every platform.', ''])

    def table(items: list[Difference]) -> None:
        lines.extend(['| Platform | Project | Difference | Baseline | Target |', '| --- | --- | --- | --- | --- |'])
        for row in items:
            details = row['change']
            if row['blocked_by']:
                details += ': ' + ', '.join(row['blocked_by'])
            cells = (row['platform'], row['project'], details, row['baseline'], row['target'])
            lines.append('| ' + ' | '.join(escape(str(x)).replace('|', '&#124;').replace('\n', ' ')
                                          for x in cells) + ' |')
        lines.append('')

    unchanged = [r for r in rows if r['change'].startswith('unchanged-') or r['change'] == 'not-applicable']
    changed = [r for r in rows if r not in unchanged]
    if changed:
        table(changed)
    else:
        lines.extend(['No observed differences.', ''])
    if unchanged:
        lines.extend([f'<details><summary>Unchanged results ({len(unchanged)})</summary>', ''])
        table(unchanged)
        lines.extend(['</details>', ''])
    for row in changed:
        for label, ids in (('New failures (passed on baseline)', row['new_failures']),
                           ('Fixed failures', row['fixed_failures']),
                           ('Newly observed failures (no baseline pass)', row['newly_observed_failures']),
                           ('Changed failure messages', row['changed_failures']),
                           ('Previously passing tests now skipped or absent', row['lost_tests'])):
            if ids:
                lines.extend([f'<details><summary>{escape(row["project"])}: {label}</summary>', '',
                              *[f'- <code>{escape(test)}</code>' for test in ids], '', '</details>', ''])
        if row['dependency_changes']:
            lines.extend([f'{escape(row["project"])}: dependency versions differ; see `comparison.json`.', ''])
        lines.extend(escape(note) + '\n' for note in row['notes'])
    lines.extend(['Full dependency-version differences and outcomes are in `comparison.json`.', ''])
    return '\n'.join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    planning = sub.add_parser('plan')
    planning.add_argument('--ref', required=True)
    planning.add_argument('--base-ref', help='Baseline ref (default: PR base branch, otherwise main)')
    planning.add_argument('--platform', nargs='+', choices=tuple(load_platforms()['platforms']), required=True)
    planning.add_argument('--mode', choices=MODES, default=DEFAULT_MODE)
    planning.add_argument('--output', type=Path, required=True)
    planning.add_argument('--project', nargs='+', choices=tuple(p['name'] for p in load_projects()))
    planning.add_argument('--github-output', type=Path)
    scheduling = sub.add_parser('schedule')
    scheduling.add_argument('--reports', type=Path, required=True)
    scheduling.add_argument('--output', type=Path, required=True)
    scheduling.add_argument('--commit', required=True)
    scheduling.add_argument('--platform', choices=tuple(load_platforms()['platforms']), required=True)
    scheduling.add_argument('--mode', choices=MODES, default=DEFAULT_MODE)
    scheduling.add_argument('--github-output', type=Path)
    scheduling.add_argument('--project', nargs='+', choices=tuple(p['name'] for p in load_projects()))
    comparing = sub.add_parser('report')
    comparing.add_argument('--plan', type=Path, required=True)
    comparing.add_argument('--artifacts', type=Path, required=True)
    comparing.add_argument('--output', type=Path, required=True)
    comparing.add_argument('--summary', type=Path)
    args = parser.parse_args()
    if args.command == 'plan':
        value = make_plan(args.ref, args.platform, args.mode, args.project, base_ref=args.base_ref)
        args.output.write_text(json.dumps(value, indent=2) + '\n')
        if args.github_output:
            emit_plan(value, args.github_output)
        return 0
    if args.command == 'schedule':
        projects = schedule(args.reports, args.output, args.commit, args.platform, args.mode, args.project)
        if args.github_output:
            with args.github_output.open('a') as stream:
                stream.write(f'projects={json.dumps(projects)}\nhas_projects={str(bool(projects)).lower()}\n')
        print(json.dumps(projects))
        return 0
    plan = cast(ComparisonPlan, json.loads(args.plan.read_text()))
    result = report(plan, args.artifacts)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'comparison.json').write_text(json.dumps(result, indent=2) + '\n')
    rendered = markdown(result)
    (args.output / 'comparison.md').write_text(rendered)
    if args.summary:
        with args.summary.open('a') as stream:
            stream.write(rendered)
    counts = cast(dict[str, int], result['counts'])
    print('Comparison results: ' + ', '.join(f'{count} {change}' for change, count in sorted(counts.items())))
    if counts.get('regression') or counts.get('newly-blocked'):
        print('Comparison failed: regressions or newly blocked projects were observed.')
        return 1
    if not result['complete'] or any(counts.get(c) for c in ('inconclusive', 'coverage-change', 'newly-observed-failure')):
        print('Comparison incomplete: interpreter preparation, missing evidence, coverage changes, '
              'or failures without a baseline pass need review. See comparison.md for details.')
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

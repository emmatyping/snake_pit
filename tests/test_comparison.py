import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from snake_pit.artifacts import verified_wheel, wheel_requirements
from snake_pit.comparison import ComparisonPlan, TestOutcome, difference, emit_plan, junit, make_plan, markdown, placeholder, report, schedule
from snake_pit.dependencies import RECIPES, load_recipes
from snake_pit.models import Phase, PhaseResult, ProjectResult
from snake_pit.project import run_project
from snake_pit.refs import parse_ref, resolve
from snake_pit.worker import sha256

PLATFORM = 'x86_64-unknown-linux-gnu'
RECIPE_HASH = hashlib.sha256(RECIPES.read_bytes()).hexdigest()


def result(name: str, commit: str = 'a' * 40, status: str = 'built', build: str = 'passed') -> ProjectResult:
    version = load_recipes()[name]['version'] if name != 'CPython' else ''
    value = placeholder(name, version, commit, PLATFORM, 'debug', '')
    value.pop('error')
    value.pop('failed_phase')
    value.update({'status': status, 'build_status': build, 'recipe_sha256': RECIPE_HASH, 'dependencies': {}})
    return value


def lock(path: Path, versions: dict[str, str]) -> None:
    path.write_text('lock-version = "1.0"\npackages = [\n' +
                    ''.join(f'{{name={json.dumps(k)},version={json.dumps(v)}}},\n' for k, v in versions.items()) + ']\n')


def write_result(folder: Path, value: ProjectResult) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'result.json').write_text(json.dumps(value))


def wheel(folder: Path, value: ProjectResult) -> Path:
    (folder / 'wheels').mkdir(parents=True, exist_ok=True)
    name = value['project'].replace('-', '_')
    path = folder / 'wheels' / f'{name}-{value["release"]}-py3-none-any.whl'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(f'{name}-{value["release"]}.dist-info/METADATA',
                         f'Metadata-Version: 2.1\nName: {value["project"]}\nVersion: {value["release"]}\n')
    value['artifacts'][path.name] = sha256(path)
    return path


def outcomes(**values: str) -> dict[str, TestOutcome]:
    return {name: {'outcome': outcome, 'message': 'failure' if outcome == 'failed' else ''}
            for name, outcome in values.items()}


class RefTests(unittest.TestCase):
    def test_ref_forms(self) -> None:
        cases = {
            'main': ('python/cpython', 'main', None),
            'a' * 40: ('python/cpython', 'a' * 40, None),
            'someone/cpython:feature/fix': ('someone/cpython', 'feature/fix', None),
            'refs/pull/123/head': ('python/cpython', 'refs/pull/123/head', 123),
            'https://github.com/python/cpython/pull/123': ('python/cpython', '123', 123),
            'https://github.com/someone/cpython/tree/feature/fix': ('someone/cpython', 'feature/fix', None),
            'https://github.com/python/cpython/commit/abc123': ('python/cpython', 'abc123', None),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(parse_ref(value), expected)

    def test_fork_pr_resolves_head_not_merge(self) -> None:
        with patch('snake_pit.refs.github_json', return_value={
            'head': {'sha': 'b' * 40, 'repo': {'full_name': 'contributor/cpython'}}
        }) as api:
            revision = resolve('https://github.com/python/cpython/pull/123')
        api.assert_called_once_with('repos/python/cpython/pulls/123')
        self.assertEqual(revision['commit'], 'b' * 40)
        self.assertEqual(revision['repository'], 'contributor/cpython')

    def test_branch_slashes_are_encoded(self) -> None:
        with patch('snake_pit.refs.github_json', return_value={'sha': 'a' * 40}) as api:
            resolve('someone/cpython:feature/fix')
        api.assert_called_once_with('repos/someone/cpython/commits/feature%2Ffix')

    def test_invalid_ref_hosts_and_unresolved_commits(self) -> None:
        for value in ('https://evil.example/a/b/tree/main', 'https://user@github.com/a/b/tree/main',
                      'file:///tmp/repo', 'a/b:bad ref', 'https://github.com/a/b/pull/nope'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_ref(value)
        with patch('snake_pit.refs.github_json', return_value={'sha': 'main'}), self.assertRaises(ValueError):
            resolve('main')

    def test_plan_pins_both_sides_once_and_deduplicates_identical_commits(self) -> None:
        revision = {'repository': 'python/cpython', 'ref': 'main', 'commit': 'a' * 40}
        with patch('snake_pit.comparison.resolve', return_value=revision) as resolver:
            plan = make_plan('main', [PLATFORM], 'debug', ['packaging'])
        resolver.assert_called_once_with('main')
        self.assertTrue(plan['same_commit'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'outputs'
            emit_plan(plan, path)
            output = dict(line.split('=', 1) for line in path.read_text().splitlines())
            self.assertEqual(len(json.loads(output['revisions'])), 1)
        with self.assertRaises(ValueError):
            make_plan('main', [PLATFORM], 'debug', ['unknown'])


class SchedulingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def add(self, name: str, dependencies: dict[str, str], status: str = 'built', build: str = 'passed') -> ProjectResult:
        value = result(name, status=status, build=build)
        value['dependencies'] = dependencies
        write_result(self.root / 'reports' / name, value)
        lock(self.root / 'reports' / name / 'pylock.toml', dependencies)
        return value

    def test_transitive_build_failure_blocks_only_matching_versions(self) -> None:
        cffi = self.add('cffi', {}, 'failed', 'failed')
        self.add('urllib3', {'cryptography': '50.0.2', 'cffi': cffi['release']})
        self.add('jinja2', {'cffi': '999.0'})
        eligible = schedule(self.root / 'reports', self.root / 'blocked', 'a' * 40, PLATFORM, 'debug',
                            ['cffi', 'urllib3', 'jinja2'])
        self.assertEqual(eligible, ['jinja2'])
        blocked = json.loads((self.root / 'blocked/urllib3/result.json').read_text())
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual(blocked['blocked_by'], ['cffi==' + cffi['release']])

    def test_test_or_test_dependency_failure_does_not_block_consumers(self) -> None:
        pytest = self.add('pytest', {}, 'failed', 'passed')
        self.add('requests', {'pytest': pytest['release']})
        self.assertEqual(schedule(self.root / 'reports', self.root / 'blocked', 'a' * 40, PLATFORM, 'debug',
                                  ['pytest', 'requests']), ['requests'])

    def test_test_dependency_cycles_do_not_create_build_cycles(self) -> None:
        recipes = load_recipes()
        self.add('pytest', {'requests': recipes['requests']['version']})
        self.add('requests', {'pytest': recipes['pytest']['version']})
        self.assertEqual(set(schedule(self.root / 'reports', self.root / 'blocked', 'a' * 40, PLATFORM, 'debug',
                                      ['pytest', 'requests'])), {'pytest', 'requests'})

    def test_all_phase_locks_are_checked_without_overwriting_versions(self) -> None:
        cffi = self.add('cffi', {}, 'failed', 'failed')
        self.add('requests', {'cffi': '999.0'})
        lock(self.root / 'reports/requests/pylock.build.toml', {'cffi': cffi['release']})
        self.assertEqual(schedule(self.root / 'reports', self.root / 'blocked', 'a' * 40, PLATFORM, 'debug',
                                  ['cffi', 'requests']), [])

    def test_missing_report_is_infrastructure_not_a_known_dependency_failure(self) -> None:
        self.add('requests', {'cffi': load_recipes()['cffi']['version']})
        self.assertEqual(schedule(self.root / 'reports', self.root / 'blocked', 'a' * 40, PLATFORM, 'debug',
                                  ['cffi', 'requests']), ['requests'])
        missing = json.loads((self.root / 'blocked/cffi/result.json').read_text())
        self.assertEqual(missing['status'], 'infrastructure-error')

    def test_platform_exclusion_keeps_provenance_and_is_not_scheduled(self) -> None:
        platform = 'x86_64-pc-windows-msvc'
        value = run_project('uvloop', 'a' * 40, platform, 'debug', self.root / 'artifact',
                            self.root / 'work', self.root / 'reports/uvloop', build_only=True)
        self.assertEqual(value['status'], 'not-applicable')
        self.assertEqual(value['recipe_sha256'], RECIPE_HASH)
        self.assertEqual(schedule(self.root / 'reports', self.root / 'blocked', 'a' * 40,
                                  platform, 'debug', ['uvloop']), [])
        skipped = json.loads((self.root / 'blocked/uvloop/result.json').read_text())
        self.assertEqual(skipped['status'], 'not-applicable')


class JUnitTests(unittest.TestCase):
    def test_duplicate_records_preserve_failures_regardless_of_order(self) -> None:
        cases = ['<testcase classname="suite" name="case"/>',
                 '<testcase classname="suite" name="case"><failure message="call failed"/></testcase>',
                 '<testcase classname="suite" name="case"><error message="teardown failed"/></testcase>']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tests.xml'
            for records in (cases, cases[::-1]):
                path.write_text('<testsuite>' + ''.join(records) + '</testsuite>')
                self.assertEqual(junit(path), {'suite::case': {'outcome': 'failed',
                                                             'message': 'call failed\nteardown failed'}})

    def test_repeated_passes_and_skips_are_compared_conservatively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tests.xml'
            path.write_text('<testsuite><testcase classname="suite" name="case"/>'
                            '<testcase classname="suite" name="case"/></testsuite>')
            self.assertEqual(junit(path)['suite::case']['outcome'], 'passed')
            path.write_text('<testsuite><testcase classname="suite" name="case"><skipped/></testcase>'
                            '<testcase classname="suite" name="case"/></testsuite>')
            self.assertEqual(junit(path)['suite::case']['outcome'], 'skipped')


class DifferenceTests(unittest.TestCase):
    def test_new_failures_are_detected_even_when_both_suites_fail(self) -> None:
        before, after = result('pytest', status='failed'), result('pytest', 'b' * 40, status='failed')
        before['failed_phase'] = after['failed_phase'] = 'test'
        delta = difference(before, after, outcomes(old='failed', regression='passed'),
                           outcomes(old='failed', regression='failed'))
        self.assertEqual(delta['change'], 'regression')
        self.assertEqual(delta['new_failures'], ['regression'])

    def test_unexecuted_baseline_case_is_not_claimed_as_a_new_regression(self) -> None:
        before, after = result('pytest', status='failed'), result('pytest', 'b' * 40, status='failed')
        delta = difference(before, after, outcomes(existing='failed'), outcomes(existing='failed', extra='failed'))
        self.assertEqual(delta['new_failures'], [])
        self.assertEqual(delta['newly_observed_failures'], ['extra'])
        self.assertEqual(delta['change'], 'newly-observed-failure')

    def test_shared_failures_and_fixed_failures(self) -> None:
        before, after = result('pytest', status='failed'), result('pytest', 'b' * 40, status='failed')
        self.assertEqual(difference(before, after, outcomes(case='failed'), outcomes(case='failed'))['change'],
                         'unchanged-failure')
        after['status'] = 'passed'
        self.assertEqual(difference(before, after, outcomes(case='failed'), outcomes(case='passed'))['fixed_failures'], ['case'])

    def test_build_regression_is_distinct_from_old_test_failure(self) -> None:
        before, after = result('pytest', status='failed'), result('pytest', 'b' * 40, status='failed', build='failed')
        before['failed_phase'], after['failed_phase'] = 'test', 'build'
        self.assertEqual(difference(before, after, {}, {})['change'], 'regression')

    def test_dependency_drift_and_missing_results_are_inconclusive(self) -> None:
        before, after = result('pytest', status='passed'), result('pytest', 'b' * 40, status='failed')
        before['dependencies'], after['dependencies'] = {'example': '1'}, {'example': '2'}
        self.assertEqual(difference(before, after, {}, {})['change'], 'inconclusive')
        after = placeholder('pytest', '1', 'b' * 40, PLATFORM, 'debug', 'missing')
        self.assertEqual(difference(before, after, {}, {})['change'], 'inconclusive')

    def test_skipping_previously_passing_tests_is_a_coverage_change(self) -> None:
        before, after = result('pytest', status='passed'), result('pytest', 'b' * 40, status='passed')
        delta = difference(before, after, outcomes(case='passed'), outcomes(case='skipped'))
        self.assertEqual(delta['change'], 'coverage-change')
        self.assertEqual(delta['lost_tests'], ['case'])


class ReportTests(unittest.TestCase):
    def plan(self) -> ComparisonPlan:
        return dict(baseline=dict(repository='python/cpython', ref='main', commit='a' * 40),
                    target=dict(repository='someone/cpython', ref='feature', commit='b' * 40),
                    mode='debug', platforms=[dict(platform=PLATFORM, runner='ubuntu-26.04')],
                    projects=['packaging'], recipes_sha256=RECIPE_HASH, same_commit=False)

    def interpreter(self, root: Path, commit: str) -> None:
        folder = root / commit
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'interpreter.json').write_text(json.dumps(result('CPython', commit, 'passed')))

    def test_report_handles_partial_junit_and_renders_changes_outside_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for commit in ('a' * 40, 'b' * 40):
                self.interpreter(root, commit)
                write_result(root / commit / 'packaging', result('packaging', commit, 'failed'))
            (root / ('a' * 40) / 'packaging/tests.xml').write_text(
                '<testsuite><testcase classname="suite" name="old"><failure message="old"/></testcase>'
                '<testcase classname="suite" name="new"/></testsuite>')
            (root / ('b' * 40) / 'packaging/tests.xml').write_text(
                '<testsuite><testcase classname="suite" name="old"><failure message="old"/></testcase>'
                '<testcase classname="suite" name="new"><failure message="new"/></testcase></testsuite>')
            value = report(self.plan(), root)
            self.assertEqual(value['counts'], {'unchanged-pass': 1, 'regression': 1})
            rendered = markdown(value)
            self.assertIn('suite::new', rendered)
            self.assertIn('Unchanged results (1)', rendered)
            self.assertIn('passed on main', rendered)

    def test_missing_interpreter_is_reported_once_and_comparison_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            value = report(self.plan(), Path(directory))
        self.assertFalse(value['complete'])
        self.assertEqual(value['counts'], {'inconclusive': 1})

    def test_same_commit_uses_one_set_of_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.interpreter(root, 'a' * 40)
            write_result(root / 'packaging', result('packaging', status='passed'))
            plan = self.plan()
            plan['target'] = plan['baseline'].copy()
            plan['same_commit'] = True
            self.assertEqual(report(plan, root)['counts'], {'unchanged-pass': 2})

    def test_truncated_junit_does_not_discard_other_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for commit in ('a' * 40, 'b' * 40):
                self.interpreter(root, commit)
                write_result(root / commit / 'packaging', result('packaging', commit, 'passed'))
            (root / ('b' * 40) / 'packaging/tests.xml').write_text('<testsuite>')
            self.assertEqual(report(self.plan(), root)['counts'], {'unchanged-pass': 1, 'inconclusive': 1})


class PreparedWheelTests(unittest.TestCase):
    def test_identity_hash_and_failed_tests_do_not_prevent_wheel_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = result('cffi', status='failed', build='passed')
            source = wheel(root, value)
            providers = {'cffi': (root, value)}
            self.assertEqual(wheel_requirements(providers, {'cffi': value['release']}, 'requests', RECIPE_HASH),
                             [source.resolve().as_uri()])
            self.assertEqual(wheel_requirements(providers, {'cffi': 'other'}, 'requests', RECIPE_HASH), [])
            source.write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                verified_wheel(root, value)

    def test_prepared_runner_reuses_wheel_and_keeps_test_failure_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / 'source'
            source.mkdir()
            (source / 'pyproject.toml').write_text('[build-system]\nrequires=["setuptools"]\n')
            archive = root / 'source.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                tar.add(source, arcname='fixture')
            build_commands: list[list[str]] = []
            version = load_recipes()['packaging']['version']
            cffi = result('cffi')
            pool = root / 'prepared'
            provider = pool / 'cffi'
            provider_wheel = wheel(provider, cffi)
            write_result(provider, cffi)

            def phase(command: Phase, cwd: Path, logs: Path, env: dict[str, str]) -> PhaseResult:
                argv = command['argv']
                name = command['name']
                if name == 'test-source':
                    Path(argv[-1]).mkdir()
                elif name == 'archive-test-source':
                    Path(next(a.split('=', 1)[1] for a in argv if a.startswith('--output='))).write_bytes(b'fixture')
                elif name.startswith('resolve-'):
                    lock(Path(argv[argv.index('-o') + 1]), {'packaging': version, 'cffi': cffi['release']})
                elif name == 'build':
                    build_commands.append(argv)
                    wheel(logs, result('packaging'))
                return dict(name=name, argv=argv, status='failed' if name == 'test' else 'passed', returncode=1 if name == 'test' else 0)

            def download(project: object, path: Path) -> None:
                path.write_bytes(archive.read_bytes())

            import contextlib
            with patch('snake_pit.project.restore', return_value=Path(sys.executable)), \
                    patch('snake_pit.project.dependencies'), patch('snake_pit.project.download', side_effect=download), \
                    patch('snake_pit.project.shutil.which', return_value='uv'), \
                    patch('snake_pit.project.subprocess.check_output', return_value=load_recipes()['packaging']['commit']), \
                    patch('snake_pit.project.run_phase', side_effect=phase), contextlib.redirect_stdout(io.StringIO()):
                prepared = run_project('packaging', 'a' * 40, PLATFORM, 'debug', root / 'artifact', root / 'build-work',
                                       pool / 'packaging', build_only=True)
                self.assertEqual(prepared['status'], 'built')
                self.assertNotIn('test', [p['name'] for p in prepared['phases']])
                tested = run_project('packaging', 'a' * 40, PLATFORM, 'debug', root / 'artifact', root / 'test-work',
                                     root / 'tested', prepared_root=pool)
            self.assertEqual(len(build_commands), 1)
            self.assertEqual(tested['status'], 'failed')
            self.assertEqual(tested['build_status'], 'passed')
            self.assertIn(provider_wheel.as_uri(), (root / 'tested/runtime-test.in').read_text())
            self.assertIn('cffi==' + cffi['release'], (root / 'tested/pylock.toml.constraints.txt').read_text())
            self.assertEqual(tested['artifacts'][next((root / 'tested/wheels').iterdir()).name],
                             prepared['artifacts'][next((pool / 'packaging/wheels').iterdir()).name])


if __name__ == '__main__':
    unittest.main()

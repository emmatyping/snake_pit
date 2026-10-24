from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest

from snake_pit.__main__ import plan
from snake_pit.dependencies import load_projects
from snake_pit.models import Job, Phase, RunResult
from snake_pit.worker import LOG_LIMIT, PHASES, inside, run_job, run_phase, sha256


class PlanningTests(unittest.TestCase):
    def test_explicit_commit_and_platform(self) -> None:
        for commit, platform in [('main', 'x86_64-unknown-linux-gnu'),
                                 ('a' * 40, 'x86_64-apple-darwin')]:
            with self.assertRaises(ValueError):
                plan(commit, platform, 'release')

    def test_platform_exclusion(self) -> None:
        for platform in ('i686-pc-windows-msvc', 'x86_64-pc-windows-msvc'):
            jobs = plan('a' * 40, platform, 'release')['jobs']
            for project in ('uvloop', 'memray'):
                self.assertEqual(next(j for j in jobs if j['project'] == project)['status'], 'not-applicable')
        for platform in ('x86_64-unknown-linux-gnu', 'aarch64-unknown-linux-gnu', 'aarch64-apple-darwin'):
            jobs = plan('a' * 40, platform, 'release')['jobs']
            self.assertEqual(next(j for j in jobs if j['project'] == 'memray')['status'], 'qualification-required')

    def test_selected_projects_have_locked_sources(self) -> None:
        projects = load_projects()
        names = {project['name'] for project in projects}
        self.assertEqual(len(projects), len(names))
        self.assertTrue({'pip', 'cython', 'sympy', 'memray', 'matplotlib', 'sphinx'} <= names)
        for project in projects:
            self.assertRegex(project['sdist']['sha256'], r'^[0-9a-f]{64}$')
            self.assertTrue(project['sdist']['url'].startswith('https://'))

    def test_plan_includes_all_candidates(self) -> None:
        jobs = plan('a' * 40, 'x86_64-unknown-linux-gnu', 'release')['jobs']
        self.assertEqual({job['project'] for job in jobs},
                         {project['name'] for project in load_projects()})


class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        lock = self.root / 'pylock.toml'
        lock.write_text('lock-version = "1.0"\ncreated-by = "test-fixture"\npackages = []\n')
        self.job: Job = dict(project='trusted-test-fixture', project_commit='b' * 40,
                        cpython_commit='a' * 40, platform='x86_64-unknown-linux-gnu', mode='release',
                        python=sys.executable, python_sha256=sha256(Path(sys.executable)),
                        worker_image='test-fixture-only', artifacts={'pylock.toml': sha256(lock)},
                        locks={'build': 'pylock.toml', 'runtime': 'pylock.toml', 'test': 'pylock.toml'},
                        phases=[dict(name=name, argv=[sys.executable, '-c', 'pass'], timeout_seconds=5)
                                for name in PHASES])

    def run_job(self) -> RunResult:
        path = self.root / 'job.json'
        path.write_text(json.dumps(self.job))
        return run_job(path, self.root / 'output')

    def test_tampered_lock_prevents_all_execution(self) -> None:
        (self.root / 'pylock.toml').write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            self.run_job()
        self.assertFalse((self.root / 'output').exists())

    def test_lock_cannot_escape_bundle(self) -> None:
        self.job['artifacts'] = {'../outside': 'c' * 64}
        with self.assertRaisesRegex(ValueError, 'outside job bundle'):
            self.run_job()

    @unittest.skipUnless(os.name == 'posix', 'requires unprivileged directory symlinks')
    def test_bundle_root_symlink_is_resolved_without_allowing_escape(self) -> None:
        bundle = self.root / 'bundle'
        bundle.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(bundle, target_is_directory=True)
        self.assertEqual(inside(alias, '.'), bundle.resolve())
        self.assertEqual(inside(alias, 'pylock.toml'), bundle.resolve() / 'pylock.toml')
        (bundle / 'escape').symlink_to(self.root, target_is_directory=True)
        for name in ('..', 'escape'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'outside job bundle'):
                inside(alias, name)

    def test_failure_retains_phase_and_skips_later_commands(self) -> None:
        self.job['phases'][1]['argv'] = [sys.executable, '-c', 'raise SystemExit(7)']
        result = self.run_job()
        self.assertEqual(result['failed_phase'], 'build')
        self.assertEqual(result['phases'][-1]['returncode'], 7)
        self.assertEqual(result['skipped_phases'], ['install', 'import', 'collect', 'test'])
        self.assertEqual(json.loads((self.root / 'output/result.json').read_text()), result)

    def test_success_records_every_phase(self) -> None:
        result = self.run_job()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(len(result['phases']), len(PHASES))

    def test_missing_executable_is_infrastructure_error(self) -> None:
        self.job['phases'][0]['argv'] = [str(self.root / 'missing-command')]
        self.assertEqual(self.run_job()['status'], 'infrastructure-error')

    def test_pytest_zero_collection_is_failure(self) -> None:
        self.job['phases'][-1].update({'framework': 'pytest', 'argv': [sys.executable, '-c', 'raise SystemExit(5)']})
        result = self.run_job()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['phases'][-1]['detail'], 'no-tests-collected')

    @unittest.skipUnless(os.name == 'posix', 'POSIX process signal semantics')
    def test_crash_is_not_reported_as_test_failure(self) -> None:
        self.job['phases'][3]['argv'] = [sys.executable, '-c', 'import os,signal; os.kill(os.getpid(),signal.SIGTERM)']
        result = self.run_job()
        self.assertEqual(result['status'], 'crashed')
        self.assertEqual(result['phases'][-1]['returncode'], -signal.SIGTERM)

    def test_timeout_is_distinct(self) -> None:
        self.job['phases'][0].update({'timeout_seconds': 0.1,
                                     'argv': [sys.executable, '-c', 'import time; time.sleep(30)']})
        result = self.run_job()
        self.assertEqual(result['status'], 'timeout')

    def test_noisy_process_is_drained_with_bounded_log(self) -> None:
        phase: Phase = dict(name='test', argv=[sys.executable, '-c',
                     f'import sys; sys.stdout.write("x" * {LOG_LIMIT + 123} + "final diagnostic")'],
                     timeout_seconds=10)
        result = run_phase(phase, self.root, self.root, dict(os.environ))
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(result['log_truncated'])
        self.assertEqual((self.root / 'test.log').stat().st_size, LOG_LIMIT)
        tail = (self.root / 'test.tail.log').read_bytes()
        self.assertEqual(len(tail), 65536)
        self.assertTrue(tail.endswith(b'final diagnostic'))

    def test_partial_output_is_saved_before_process_exits(self) -> None:
        release = self.root / 'release'
        phase: Phase = dict(name='streaming', argv=[sys.executable, '-c',
            'import pathlib, sys, time; print("diagnostic", flush=True); '
            'release = pathlib.Path(sys.argv[1]); '
            '\nwhile not release.exists(): time.sleep(0.01)', str(release)], timeout_seconds=10)
        log = self.root / 'streaming.log'
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(run_phase, phase, self.root, self.root, dict(os.environ))
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if log.exists() and b'diagnostic' in log.read_bytes():
                        break
                    time.sleep(0.01)
                self.assertFalse(pending.done())
                self.assertIn(b'diagnostic', log.read_bytes())
            finally:
                release.touch()
            self.assertEqual(pending.result()['status'], 'passed')


if __name__ == '__main__':
    unittest.main()

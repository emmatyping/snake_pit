import contextlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from snake_pit.__main__ import load_platforms, main
from snake_pit.actions import build, interpreter, matrices, restore
from snake_pit.dependencies import load_projects, load_recipes
from snake_pit.models import Phase, PhaseResult
from snake_pit.modes import MODES, parse_mode
from snake_pit.project import run_project


class ActionsTests(unittest.TestCase):
    def test_matrix_has_one_build_per_platform_and_all_test_pairs(self) -> None:
        platforms = list(load_platforms()['platforms'])
        value = matrices('a' * 40, platforms, 'debug')
        self.assertEqual(len(value['builds']), 5)
        self.assertEqual(len(value['tests']), 140)
        self.assertEqual({(t['platform'], t['project']) for t in value['tests']},
                         {(platform, p['name']) for platform in platforms for p in load_projects()})

    def test_rejects_invalid_or_duplicate_targets_and_unsupported_build_mode(self) -> None:
        for platforms, mode in [([], 'debug'), (['unknown'], 'debug'),
                                (['x86_64-unknown-linux-gnu'] * 2, 'debug'),
                                (['i686-pc-windows-msvc'], 'release-freethreading')]:
            with self.assertRaises(ValueError):
                matrices('a' * 40, platforms, mode)

    def test_argparse_rejects_platform_with_choices(self) -> None:
        error = io.StringIO()
        with patch('sys.argv', ['snake-pit', 'plan', '--commit', 'a' * 40, '--platform', 'unknown']), \
                contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn('invalid choice', error.getvalue())
        self.assertIn('i686-pc-windows-msvc', error.getvalue())

    def test_plan_defaults_to_debug(self) -> None:
        output = io.StringIO()
        with patch('sys.argv', ['snake-pit', 'plan', '--commit', 'a' * 40,
                                '--platform', 'x86_64-unknown-linux-gnu']), \
                contextlib.redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(output.getvalue())['mode'], 'debug')

    def test_build_modes_select_native_flags_and_interpreter(self) -> None:
        for mode in MODES:
            config = parse_mode(mode)
            for platform in ('x86_64-unknown-linux-gnu', 'x86_64-pc-windows-msvc'):
                with self.subTest(mode=mode, platform=platform), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    source = root / 'cpython'
                    source.mkdir()
                    (source / 'configure').write_text('--enable-safety --enable-slower-safety')
                    props = source / 'PCbuild/pyproject.props'
                    props.parent.mkdir()
                    props.write_text('<_DebugPreprocessorDefinition>NDEBUG;</_DebugPreprocessorDefinition>')
                    python = interpreter(source, platform, mode)
                    python.parent.mkdir(parents=True, exist_ok=True)
                    python.write_bytes(b'fixture interpreter')
                    with patch('snake_pit.actions.subprocess.check_output', return_value='a' * 40), \
                            patch('snake_pit.actions.dependencies'), patch('snake_pit.actions.call') as commands, \
                            patch('snake_pit.actions.subprocess.run'):
                        build(source, root / 'artifact', 'a' * 40, platform, mode)
                    argv = commands.call_args_list[0].args[0]
                    self.assertEqual('--disable-gil' in argv, config.freethreading)
                    if 'windows' in platform:
                        self.assertEqual('-d' in argv, config.debug)
                        self.assertEqual('Release' in argv, not config.debug)
                        response = source / 'PCbuild/snake-pit-assertions.rsp'
                        self.assertEqual(response.exists(), config.assertions and not config.debug)
                        if response.exists():
                            self.assertIn('@PCbuild/snake-pit-assertions.rsp', argv)
                            self.assertEqual(response.read_text(), '/p:_DebugPreprocessorDefinition=\n')
                        self.assertEqual(python.name, 'python_d.exe' if config.debug else 'python.exe')
                        self.assertEqual(python.parent.name, 'amd64t' if config.freethreading else 'amd64')
                    else:
                        self.assertEqual('--with-pydebug' in argv, config.debug)
                        self.assertEqual('--without-pydebug' in argv, not config.debug)
                        self.assertEqual('--enable-slower-safety' in argv, config.debug)
                        if not config.debug:
                            self.assertIn('--with-assertions' if config.assertions else '--without-assertions', argv)

    def test_all_freethreading_modes_reject_windows_x86(self) -> None:
        for mode in MODES:
            with self.subTest(mode=mode):
                if mode.endswith('-freethreading'):
                    with self.assertRaisesRegex(ValueError, 'Windows x86'):
                        matrices('a' * 40, ['i686-pc-windows-msvc'], mode)
                else:
                    self.assertEqual(len(matrices('a' * 40, ['i686-pc-windows-msvc'], mode)['builds']), 1)

    def test_every_selected_project_has_an_explicit_recipe(self) -> None:
        self.assertEqual(set(load_recipes()), {p['name'] for p in load_projects()})

    def test_windows_interpreter_layouts(self) -> None:
        self.assertEqual(interpreter(Path('source'), 'i686-pc-windows-msvc', 'debug'),
                         Path('source/PCbuild/win32/python_d.exe'))
        self.assertEqual(interpreter(Path('source'), 'x86_64-pc-windows-msvc', 'release-freethreading'),
                         Path('source/PCbuild/amd64t/python.exe'))

    def test_build_archives_generated_headers_and_restores_verified_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'cpython'
            source.mkdir()
            (source / '.git').mkdir()
            (source / '.git/secret').write_text('must not be archived')
            (source / 'configure').write_text('--enable-safety --enable-slower-safety')
            python = interpreter(source, 'x86_64-unknown-linux-gnu', 'debug')
            python.parent.mkdir(parents=True)
            python.write_bytes(b'trusted fixture interpreter')
            (source / 'pyconfig.h').write_text('generated target header')
            artifact = root / 'artifact'
            with patch('snake_pit.actions.subprocess.check_output', return_value='a' * 40), \
                    patch('snake_pit.actions.dependencies') as deps, patch('snake_pit.actions.call') as commands, \
                    patch('snake_pit.actions.subprocess.run'):
                build(source, artifact, 'a' * 40, 'x86_64-unknown-linux-gnu', 'debug')
            deps.assert_called_once_with(source.resolve(), 'x86_64-unknown-linux-gnu')
            self.assertIn('--with-pydebug', commands.call_args_list[0].args[0])
            with tarfile.open(artifact / 'cpython.tar.gz') as tar:
                self.assertIn('cpython/pyconfig.h', tar.getnames())
                self.assertNotIn('cpython/.git', tar.getnames())
            restored = restore(artifact, source, 'a' * 40, 'x86_64-unknown-linux-gnu', 'debug')
            self.assertEqual(restored.read_bytes(), b'trusted fixture interpreter')
            with self.assertRaisesRegex(ValueError, 'provenance'):
                restore(artifact, source, 'b' * 40, 'x86_64-unknown-linux-gnu', 'debug')
            with (artifact / 'cpython.tar.gz').open('ab') as stream:
                stream.write(b'tamper')
            with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                restore(artifact, source, 'a' * 40, 'x86_64-unknown-linux-gnu', 'debug')

    def test_project_records_collection_failure_without_running_tests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'results'
            work = root / 'work'
            sdist_root = root / 'fixture-1.0'
            sdist_root.mkdir()
            (sdist_root / 'pyproject.toml').write_text('[build-system]\nrequires=[]\n')
            source_tar = root / 'source.tar.gz'
            with tarfile.open(source_tar, 'w:gz') as tar:
                tar.add(sdist_root, arcname=sdist_root.name)

            def download_fixture(project: object, destination: Path) -> None:
                destination.write_bytes(source_tar.read_bytes())

            def phase_fixture(phase: Phase, cwd: Path, logs: Path, env: dict[str, str]) -> PhaseResult:
                argv = phase['argv']
                if phase['name'] == 'test-source':
                    Path(argv[-1]).mkdir()
                elif phase['name'] == 'archive-test-source':
                    Path(next(arg.split('=', 1)[1] for arg in argv if arg.startswith('--output='))).write_text('archive')
                elif phase['name'] == 'build':
                    wheels = Path(argv[argv.index('--out-dir') + 1])
                    wheels.mkdir()
                    (wheels / 'fixture.whl').write_text('trusted fixture')
                elif phase['name'] in ('resolve-tools', 'resolve-build', 'resolve-tests'):
                    Path(argv[-1]).write_text('lock-version="1.0"\npackages=[]\n')
                return {'name': phase['name'], 'argv': argv,
                        'status': 'failed' if phase['name'] == 'collect' else 'passed', 'returncode': 5}

            with patch('snake_pit.project.restore', return_value=Path(sys.executable)), \
                    patch('snake_pit.project.dependencies'), patch('snake_pit.project.download', download_fixture), \
                    patch('snake_pit.project.shutil.which', return_value='uv'), \
                    patch('snake_pit.project.subprocess.check_output', return_value=load_recipes()['packaging']['commit']), \
                    patch('snake_pit.project.run_phase', side_effect=phase_fixture), contextlib.redirect_stdout(io.StringIO()):
                result = run_project('packaging', 'a' * 40, 'x86_64-unknown-linux-gnu', 'debug',
                                     root / 'artifact', work, output)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['failed_phase'], 'collect')
            self.assertNotIn('test', [phase['name'] for phase in result['phases']])
            self.assertEqual(json.loads((output / 'result.json').read_text()), result)
            self.assertIn('pylock.toml', result['artifacts'])
            self.assertTrue((output / 'wheels/fixture.whl').exists())
            self.assertIn('setuptools', (output / 'runtime-test.in').read_text().splitlines())
            self.assertIn('wheel', (output / 'runtime-test.in').read_text().splitlines())

    def test_unsupported_project_platform_pair_needs_no_build_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_project('uvloop', 'a' * 40, 'i686-pc-windows-msvc', 'debug',
                                 root / 'missing', root / 'work', root / 'results')
            self.assertEqual(result['status'], 'not-applicable')
            self.assertEqual(result['phases'], [])


if __name__ == '__main__':
    unittest.main()

"""Build and test one locked project using an Actions CPython build artifact."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tomllib
from typing import NotRequired, TypedDict, cast
import urllib.request

from .__main__ import ROOT, load_platforms, plan
from .actions import dependencies, restore
from .dependencies import load_projects, load_recipes
from .models import Phase, PhaseResult, Project
from .modes import DEFAULT_MODE, MODES
from .worker import run_phase, sha256


class ProjectResult(TypedDict):
    project: str
    release: str
    cpython_commit: str
    platform: str
    mode: str
    status: str
    started_at: str
    phases: list[PhaseResult]
    artifacts: dict[str, str]
    failed_phase: NotRequired[str]
    error: NotRequired[str]
    project_commit: NotRequired[str]
    test_selection: NotRequired[list[str]]
    python_sha256: NotRequired[str]
    recipe_sha256: NotRequired[str]


class PhaseFailed(Exception):
    pass


def environment() -> dict[str, str]:
    names = ('PATH', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR', 'HOME', 'USERPROFILE',
             'INCLUDE', 'LIB', 'LIBPATH', 'VCINSTALLDIR', 'VSINSTALLDIR', 'VCToolsInstallDir',
             'WindowsSdkDir', 'WindowsSDKVersion', 'VSCMD_ARG_TGT_ARCH', 'PKG_CONFIG_PATH')
    env = {key: os.environ[key] for key in names if key in os.environ}
    env.update(PYTHONFAULTHANDLER='1', PYTHONUNBUFFERED='1', UV_NO_CONFIG='1',
               UV_PYTHON_DOWNLOADS='never', UV_DEFAULT_INDEX='https://pypi.org/simple',
               CARGO_TERM_COLOR='never', OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1')
    return env


def download(project: Project, destination: Path) -> None:
    sdist = project['sdist']
    request = urllib.request.Request(sdist['url'], headers={'User-Agent': 'snake-pit/0.1'})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open('wb') as stream:
        size = 0
        while chunk := response.read(65536):
            size += len(chunk)
            if size > 256 * 1024 * 1024:
                raise ValueError('sdist exceeds 256 MiB')
            stream.write(chunk)
    if sha256(destination) != sdist['sha256']:
        raise ValueError('locked sdist digest mismatch')


def run_project(project_name: str, commit: str, platform: str, mode: str,
                artifact: Path, work: Path, output: Path) -> ProjectResult:
    selected = next(p for p in load_projects() if p['name'] == project_name)
    planned = next(p for p in plan(commit, platform, mode)['jobs'] if p['project'] == project_name)
    output = output.resolve()
    work = work.resolve()
    output.mkdir(parents=True, exist_ok=False)
    work.mkdir(parents=True, exist_ok=False)
    result: ProjectResult = dict(project=project_name, release=selected['release'], cpython_commit=commit,
                                 platform=platform, mode=mode, status='running',
                                 started_at=datetime.now(timezone.utc).isoformat(), phases=[], artifacts={})

    def save() -> None:
        for path in output.glob('pylock*.toml'):
            result['artifacts'][path.name] = sha256(path)
        temp = output / 'result.tmp'
        temp.write_text(json.dumps(result, indent=2) + '\n')
        temp.replace(output / 'result.json')

    current_stage = 'restore'
    env = environment()

    def stage(name: str, argv: list[str], cwd: Path = work, framework: str = '',
              timeout: float = 1200) -> None:
        nonlocal current_stage
        current_stage = name
        phase: Phase = dict(name=name, argv=argv, cwd='.', timeout_seconds=timeout, framework=framework)
        print(f'{name}: {subprocess.list2cmdline(argv)}', flush=True)
        outcome = run_phase(phase, cwd, output, env)
        result['phases'].append(outcome)
        save()
        if outcome['status'] != 'passed':
            result['status'] = outcome['status']
            result['failed_phase'] = name
            log = output / f'{name}.log'
            if log.with_suffix('.tail.log').exists():
                log = log.with_suffix('.tail.log')
            if log.exists():
                print(log.read_text(errors='replace')[-12000:], flush=True)
            raise PhaseFailed(name)

    save()
    if planned['status'] == 'not-applicable':
        result['status'] = 'not-applicable'
        save()
        return result
    try:
        recipe = load_recipes()[project_name]
        result['recipe_sha256'] = sha256(ROOT / 'data/recipes.toml')
        python = restore(artifact, ROOT / 'cpython', commit, platform, mode)
        result['python_sha256'] = sha256(python)
        # Restore the same shared system dependencies as the build runner.
        current_stage = 'system-dependencies'
        dependencies(ROOT / 'cpython', platform)
        if 'linux' in platform and recipe.get('linux'):
            stage('native-dependencies', ['sudo', 'apt-get', 'install', '-y', *recipe['linux']])
        elif 'apple' in platform and recipe.get('macos'):
            stage('native-dependencies', ['brew', 'install', *recipe['macos']])
        if platform == 'i686-pc-windows-msvc':
            stage('rust-target', ['rustup', 'target', 'add', 'i686-pc-windows-msvc'])
            env['CARGO_BUILD_TARGET'] = 'i686-pc-windows-msvc'
        uv = shutil.which('uv')
        if uv is None:
            raise ValueError('uv is required on the worker PATH')
        venv = work / 'venv'
        stage('venv', [uv, 'venv', '--python', str(python), str(venv)])
        target = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        env.update(PYO3_PYTHON=str(target), PYTHON_SYS_EXECUTABLE=str(target))

        current_stage = 'source-download'
        sdist = work / selected['sdist']['filename']
        download(selected, sdist)
        result['artifacts']['sdist'] = sha256(sdist)
        source_dir = work / 'sdist'
        source_dir.mkdir()
        with tarfile.open(sdist) as tar:
            tar.extractall(source_dir, filter='data')
        roots = list(source_dir.iterdir())
        if len(roots) != 1 or not roots[0].is_dir():
            raise ValueError('expected a single root directory in sdist')
        source = roots[0]
        checkout = source
        if recipe.get('source') != 'sdist':
            tag = recipe['tag'].format(version=selected['release'],
                                       version_underscores=selected['release'].replace('.', '_'))
            checkout = work / 'upstream'
            stage('test-source', ['git', 'clone', '--depth=1', '--branch', tag,
                                  recipe['repository'] + '.git', str(checkout)])
            current_stage = 'test-source-identity'
            actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=checkout,
                                             env=env, text=True).strip()
            result['project_commit'] = actual
            if actual != recipe['commit']:
                raise ValueError('recipe version does not match its test-source commit; review the pin after upgrades')
            archive = output / 'test-source.tar'
            stage('archive-test-source', ['git', 'archive', '--format=tar', f'--output={archive}', 'HEAD'], checkout)
            result['artifacts'][archive.name] = sha256(archive)
            # The hash/commit are retained without uploading a second copy of the source tree.
            archive.unlink()

        current_stage = 'build-requirements'
        pyproject = source / 'pyproject.toml'
        config = tomllib.loads(pyproject.read_text()) if pyproject.exists() else {}
        build_requires = cast(list[str], config.get('build-system', {}).get('requires', ['setuptools', 'wheel']))
        # Seed the common backend before building any source-only build dependencies.
        # Keep these tools in the final lock: pip sync would otherwise uninstall them
        # before compiling transitive dependencies such as MarkupSafe.
        tools_input = output / 'tools.in'
        tools_input.write_text('setuptools\nwheel\n')
        tools_lock = output / 'pylock.tools.toml'
        stage('resolve-tools', [uv, 'pip', 'compile', '--python', str(target),
                                str(tools_input), '-o', str(tools_lock)])
        stage('bootstrap-tools', [uv, 'pip', 'sync', '--python', str(target), str(tools_lock)])
        build_requires = [*build_requires, 'setuptools', 'wheel', *recipe.get('build_dependencies', [])]
        build_input = output / 'build.in'
        build_input.write_text('\n'.join(build_requires) + '\n')
        build_lock = output / 'pylock.build.toml'
        stage('resolve-build', [uv, 'pip', 'compile', '--python', str(target), '--prerelease=allow',
                                '--no-build-isolation', str(build_input), '-o', str(build_lock)])
        stage('bootstrap', [uv, 'pip', 'sync', '--python', str(target), '--no-build-isolation', str(build_lock)])
        if (source / 'Cargo.lock').exists():
            stage('cargo-fetch', ['cargo', 'fetch', '--locked', '--manifest-path', str(source / 'Cargo.toml')])
            result['artifacts']['Cargo.lock'] = sha256(source / 'Cargo.lock')
            env['CARGO_NET_OFFLINE'] = 'true'
        wheels = output / 'wheels'
        stage('build', [uv, 'build', '--wheel', '--python', str(target), '--no-build-isolation',
                         '--out-dir', str(wheels), str(sdist)])
        built = list(wheels.glob('*.whl'))
        if len(built) != 1:
            raise ValueError('expected exactly one wheel from project build')
        result['artifacts'][built[0].name] = sha256(built[0])
        inputs = output / 'runtime-test.in'
        inputs.write_text('\n'.join([built[0].as_uri(), *build_requires,
                                      *[d.format(version=selected['release']) for d in recipe['dependencies']]]) + '\n')
        lock = output / 'pylock.toml'
        stage('resolve-tests', [uv, 'pip', 'compile', '--python', str(target), '--no-build-isolation',
                               '--prerelease=allow', str(inputs), '-o', str(lock)])
        stage('install', [uv, 'pip', 'sync', '--python', str(target), '--no-build-isolation', str(lock)])
        module = recipe['module']
        stage('import', [str(target), '-c', f'import {module}; print({module}.__file__)'])

        if recipe.get('adapter') == 'django':
            selection = recipe.get('selection', [])
            command = [str(target), str(checkout / 'tests/runtests.py'), '--settings=test_sqlite', *selection]
            result['test_selection'] = selection
            stage('test', command)
        else:
            if recipe.get('installed'):
                location = subprocess.check_output([str(target), '-c',
                    f'import {module}, pathlib; print(pathlib.Path({module}.__file__).parent)'], cwd=work,
                    env=env, text=True).strip()
                tests = Path(location) / recipe['tests']
            else:
                tests = checkout / recipe['tests']
            result['test_selection'] = [str(tests)]
            # Load upstream configuration/plugins but don't add source packages to sys.path.
            command = [str(target), '-m', 'pytest', '--import-mode=importlib', '-o', 'pythonpath=', str(tests)]
            stage('collect', [*command, '--collect-only', '-qq'], framework='pytest')
            stage('test', [*command, f'--junitxml={output / "tests.xml"}'], framework='pytest', timeout=1800)
        result['status'] = 'passed'
    except PhaseFailed:
        pass
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        result.update({'status': 'failed', 'failed_phase': current_stage, 'error': str(exc)})
    finally:
        save()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--platform', choices=tuple(load_platforms()['platforms']), required=True)
    parser.add_argument('--project', choices=tuple(p['name'] for p in load_projects()), required=True)
    parser.add_argument('--mode', choices=MODES, default=DEFAULT_MODE)
    parser.add_argument('--artifact', type=Path, default=Path('cpython-artifact'))
    parser.add_argument('--work', type=Path, default=Path('project-work'))
    parser.add_argument('--output', type=Path, default=Path('results'))
    args = parser.parse_args()
    result = run_project(args.project, args.commit, args.platform, args.mode, args.artifact, args.work, args.output)
    print(json.dumps(result, indent=2))
    return 0 if result['status'] in ('passed', 'not-applicable') else 1


if __name__ == '__main__':
    raise SystemExit(main())

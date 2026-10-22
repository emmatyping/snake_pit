"""Build and test one locked project using an Actions CPython build artifact."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tomllib
from typing import cast
import urllib.request

from .__main__ import ROOT, load_platforms, plan
from .actions import dependencies, restore
from .artifacts import locked_versions, read_results, verified_wheel, wheel_requirements
from .dependencies import load_projects, load_recipes
from .models import Phase, PhaseResult, Project, ProjectResult
from .modes import DEFAULT_MODE, MODES, parse_mode
from .worker import run_phase, sha256


class PhaseFailed(Exception):
    pass


def environment() -> dict[str, str]:
    names = ('PATH', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR', 'HOME', 'USERPROFILE',
             'ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432', 'SystemDrive', 'APPDATA', 'LOCALAPPDATA', 'COMSPEC',
             'INCLUDE', 'LIB', 'LIBPATH', 'VCINSTALLDIR', 'VSINSTALLDIR', 'VCToolsInstallDir',
             'WindowsSdkDir', 'WindowsSDKVersion', 'VSCMD_ARG_TGT_ARCH', 'PKG_CONFIG_PATH')
    env = {key: os.environ[key] for key in names if key in os.environ}
    env.update(PYTHONFAULTHANDLER='1', PYTHONUNBUFFERED='1', PYTHONUTF8='1', UV_NO_CONFIG='1',
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


def windows_compiler_environment(platform: str, work: Path, env: dict[str, str]) -> None:
    """Configure the native compiler for the target, including VS 2026 runners."""
    installer = Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Microsoft Visual Studio/Installer'
    installation = subprocess.check_output([str(installer / 'vswhere.exe'), '-latest', '-products', '*',
        '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-property', 'installationPath'],
        text=True).strip()
    if not installation:
        raise ValueError('Visual C++ toolchain was not found')
    arch = 'x86' if platform.startswith('i686') else 'amd64'
    script = work / 'compiler-environment.cmd'
    developer = Path(installation) / 'Common7/Tools/VsDevCmd.bat'
    script.write_text(f'@echo off\ncall "{developer}" -no_logo -arch={arch} -host_arch=amd64\n'
                      'if errorlevel 1 exit /b %errorlevel%\nset\n')
    settings = subprocess.check_output([os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/c', str(script)], text=True)
    allowed = {'PATH', 'INCLUDE', 'LIB', 'LIBPATH', 'VCINSTALLDIR', 'VSINSTALLDIR', 'VCTOOLSINSTALLDIR',
               'WINDOWSSDKDIR', 'WINDOWSSDKVERSION', 'VSCMD_ARG_TGT_ARCH', 'UNIVERSALCRTSDKDIR', 'UCRTVERSION'}
    for line in settings.splitlines():
        name, separator, value = line.partition('=')
        if separator and name.upper() in allowed:
            env[name.upper()] = value
    # Explicit compilers also bypass Meson's auto-detected Strawberry Perl ccache,
    # which is broken on the current hosted Windows image.
    env.update(DISTUTILS_USE_SDK='1', MSSdk='1', CC='cl', CXX='cl')


def lxml_command(python: Path, checkout: Path) -> list[str]:
    wrapper = ('import lxml, lxml.html, pathlib, runpy, sys; lxml.__path__.append(sys.argv[1]); '
               'lxml.html.__path__.append(str(pathlib.Path(sys.argv[1]) / "html")); '
               'runner = sys.argv[2]; sys.argv = sys.argv[2:]; '
               'runpy.run_path(runner, run_name="__main__")')
    return [str(python), '-P', '-c', wrapper, str(checkout / 'src/lxml'),
            str(checkout / 'test.py'), '--no-src']


def run_project(project_name: str, commit: str, platform: str, mode: str,
                artifact: Path, work: Path, output: Path, *, build_only: bool = False,
                prepared_root: Path | None = None, baseline_root: Path | None = None) -> ProjectResult:
    selected = next(p for p in load_projects() if p['name'] == project_name)
    planned = next(p for p in plan(commit, platform, mode)['jobs'] if p['project'] == project_name)
    output = output.resolve()
    work = work.resolve()
    output.mkdir(parents=True, exist_ok=False)
    work.mkdir(parents=True, exist_ok=False)
    result: ProjectResult = dict(project=project_name, release=selected['release'], cpython_commit=commit,
                                 platform=platform, mode=mode, status='running',
                                 started_at=datetime.now(timezone.utc).isoformat(), phases=[], artifacts={},
                                 build_status='not-run', recipe_sha256=sha256(ROOT / 'data/recipes.toml'))

    def save() -> None:
        result['dependencies'] = locked_versions(output / 'pylock.toml')
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
            if prepared_root is None and name in ('build', 'bootstrap', 'bootstrap-backends') and outcome['status'] in ('failed', 'crashed', 'timeout'):
                result['build_status'] = 'failed'
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
        current_stage = 'prepared-artifact'
        prepared = read_results(prepared_root, commit, platform, mode) if prepared_root is not None else {}
        own_prepared = prepared.get(project_name)
        if prepared_root is not None:
            if own_prepared is None or own_prepared[1].get('build_status') != 'passed':
                raise ValueError('a verified prepared source wheel is required')
            if own_prepared[1].get('recipe_sha256') != result['recipe_sha256']:
                raise ValueError('prepared source wheel recipe mismatch')
            result['build_status'] = 'passed'
        reference = own_prepared[0] if own_prepared is not None else baseline_root

        def compile_options(input_path: Path, lock_name: str) -> list[str]:
            versions = locked_versions(reference / lock_name) if reference is not None else {}
            if not versions:
                return []
            wheels = wheel_requirements(prepared, versions, project_name, result['recipe_sha256'])
            if wheels:
                with input_path.open('a') as stream:
                    stream.write('\n'.join(wheels) + '\n')
            constraints = output / (lock_name + '.constraints.txt')
            constraints.write_text(''.join(f'{name}=={version}\n' for name, version in sorted(versions.items())))
            return ['--constraint', str(constraints)]

        current_stage = 'restore'
        python = restore(artifact, ROOT / 'cpython', commit, platform, mode)
        result['python_sha256'] = sha256(python)
        # Restore the same shared system dependencies as the build runner.
        current_stage = 'system-dependencies'
        dependencies(ROOT / 'cpython', platform)
        if 'windows' in platform:
            current_stage = 'compiler-environment'
            windows_compiler_environment(platform, work, env)
            if recipe.get('adapter') == 'lxml':
                # Use upstream's architecture-aware Windows dependency bundle,
                # including matching static XML/XSLT/iconv/zlib libraries.
                env['STATIC_DEPS'] = 'true'
        if 'linux' in platform and recipe.get('linux'):
            stage('native-dependencies', ['sudo', 'apt-get', 'install', '-y', *recipe['linux']])
        elif 'apple' in platform and recipe.get('macos'):
            stage('native-dependencies', ['brew', 'install', *recipe['macos']])
        elif 'windows' in platform and recipe.get('windows'):
            # Build static libraries with the same dynamic CRT as target CPython;
            # this avoids depending on extra DLL search paths in each test process.
            arch = 'x86' if platform.startswith('i686') else 'x64'
            build_type = 'debug' if parse_mode(mode).debug else 'release'
            triplet = f'{arch}-snake-pit'
            triplets = work / 'triplets'
            triplets.mkdir()
            (triplets / f'{triplet}.cmake').write_text(
                f'set(VCPKG_TARGET_ARCHITECTURE {arch})\nset(VCPKG_CRT_LINKAGE dynamic)\n'
                f'set(VCPKG_LIBRARY_LINKAGE static)\nset(VCPKG_BUILD_TYPE {build_type})\n')
            native = work / 'native'
            vcpkg = Path(os.environ.get('VCPKG_INSTALLATION_ROOT', 'C:/vcpkg')) / 'vcpkg.exe'
            stage('native-dependencies', [str(vcpkg), 'install', *recipe['windows'],
                f'--triplet={triplet}', f'--overlay-triplets={triplets}', f'--x-install-root={native}'])
            prefix = native / triplet
            env['INCLUDE'] = str(prefix / 'include') + ';' + env.get('INCLUDE', '')
            libraries = prefix / ('debug/lib' if build_type == 'debug' else 'lib')
            # Pillow searches these conventional names; vcpkg's static builds
            # use zs/zsd and jpeg-static instead. Keep the originals for CMake.
            for alias, candidates in {'zlib.lib': ('zs.lib', 'zsd.lib', 'zlibstatic.lib', 'zlibstaticd.lib'),
                                      'jpeg.lib': ('jpeg-static.lib',)}.items():
                if not (libraries / alias).exists():
                    for candidate in candidates:
                        if (libraries / candidate).exists():
                            shutil.copyfile(libraries / candidate, libraries / alias)
                            break
            env['LIB'] = str(libraries) + ';' + env.get('LIB', '')
        if platform == 'i686-pc-windows-msvc':
            stage('rust-target', ['rustup', 'target', 'add', 'i686-pc-windows-msvc'])
            env['CARGO_BUILD_TARGET'] = 'i686-pc-windows-msvc'
        uv = shutil.which('uv')
        if uv is None:
            raise ValueError('uv is required on the worker PATH')
        venv = work / 'venv'
        stage('venv', [uv, 'venv', '--python', str(python), str(venv)])
        scripts = venv / ('Scripts' if os.name == 'nt' else 'bin')
        target = scripts / ('python.exe' if os.name == 'nt' else 'python')
        env.update(PATH=str(scripts) + os.pathsep + env.get('PATH', ''), VIRTUAL_ENV=str(venv),
                   PYO3_PYTHON=str(target), PYTHON_SYS_EXECUTABLE=str(target))

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
        if recipe.get('source') != 'sdist' and not build_only:
            tag = recipe['tag'].format(version=selected['release'],
                                       version_underscores=selected['release'].replace('.', '_'))
            checkout = work / 'upstream'
            stage('test-source', ['git', '-c', 'core.autocrlf=false', 'clone', '--depth=1', '--branch', tag,
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
        backend_names = {'setuptools', 'wheel', 'meson-python', 'meson', 'ninja', 'cython', 'pybind11',
                         'setuptools-scm', 'setuptools-rust', 'maturin'}
        tools = ['setuptools', 'wheel', *recipe.get('build_dependencies', [])]
        for requirement in build_requires:
            name = re.match(r'[A-Za-z0-9_.-]+', requirement)
            if name and name[0].lower().replace('_', '-') in backend_names:
                tools.append(requirement)
        tools_input = output / 'tools.in'
        tools_input.write_text('setuptools\nwheel\n')
        tools_lock = output / 'pylock.tools.toml'
        stage('resolve-tools', [uv, 'pip', 'compile', '--python', str(target),
                                str(tools_input), '-o', str(tools_lock),
                                *compile_options(tools_input, tools_lock.name)])
        stage('bootstrap-tools', [uv, 'pip', 'sync', '--python', str(target), '--only-binary=:all:', str(tools_lock)])
        backends_input = output / 'backends.in'
        backends_input.write_text('\n'.join(tools) + '\n')
        backends_lock = output / 'pylock.backends.toml'
        stage('resolve-backends', [uv, 'pip', 'compile', '--python', str(target), '--no-build-isolation',
                                   str(backends_input), '-o', str(backends_lock),
                                   *compile_options(backends_input, backends_lock.name)])
        stage('bootstrap-backends', [uv, 'pip', 'sync', '--python', str(target), '--no-build-isolation', str(backends_lock)])
        build_requires = [*build_requires, 'setuptools', 'wheel', *recipe.get('build_dependencies', [])]
        build_input = output / 'build.in'
        build_input.write_text('\n'.join(build_requires) + '\n')
        build_lock = output / 'pylock.build.toml'
        stage('resolve-build', [uv, 'pip', 'compile', '--python', str(target), '--prerelease=allow',
                                '--no-build-isolation', str(build_input), '-o', str(build_lock),
                                *compile_options(build_input, build_lock.name)])
        stage('bootstrap', [uv, 'pip', 'sync', '--python', str(target), '--no-build-isolation', str(build_lock)])
        if own_prepared is None and (source / 'Cargo.lock').exists():
            stage('cargo-fetch', ['cargo', 'fetch', '--locked', '--manifest-path', str(source / 'Cargo.toml')])
            result['artifacts']['Cargo.lock'] = sha256(source / 'Cargo.lock')
            env['CARGO_NET_OFFLINE'] = 'true'
        wheels = output / 'wheels'
        if own_prepared is None:
            stage('build', [uv, 'build', '--wheel', '--python', str(target), '--no-build-isolation',
                             '--out-dir', str(wheels), str(sdist)])
        else:
            wheel = verified_wheel(*own_prepared)
            wheels.mkdir()
            shutil.copyfile(wheel, wheels / wheel.name)
            result['phases'].append(dict(name='build', argv=['reuse-prepared-wheel', wheel.name], status='passed'))
        built = list(wheels.glob('*.whl'))
        if len(built) != 1:
            raise ValueError('expected exactly one wheel from project build')
        result['artifacts'][built[0].name] = sha256(built[0])
        result['build_status'] = 'passed'
        save()
        inputs = output / 'runtime-test.in'
        inputs.write_text('\n'.join([built[0].as_uri(), *build_requires,
                                      *[d.format(version=selected['release']) for d in recipe['dependencies']]]) + '\n')
        lock = output / 'pylock.toml'
        stage('resolve-tests', [uv, 'pip', 'compile', '--python', str(target), '--no-build-isolation',
                               '--prerelease=allow', str(inputs), '-o', str(lock),
                               *compile_options(inputs, lock.name)])
        if build_only:
            result['status'] = 'built'
            return result
        stage('install', [uv, 'pip', 'sync', '--python', str(target), '--no-build-isolation', str(lock)])
        module = recipe['module']
        stage('import', [str(target), '-c', f'import {module}; print({module}.__file__)'])
        env.update(recipe.get('test_env', {}))

        if recipe.get('adapter') == 'django':
            selection = recipe.get('selection', [])
            command = [str(target), str(checkout / 'tests/runtests.py'), '--settings=test_sqlite', *selection]
            result['test_selection'] = selection
            stage('test', command)
        elif recipe.get('adapter') == 'lxml':
            # lxml builds suites through test_suite(); pytest also collects its
            # uninitialized base classes. Keep the built extensions installed and
            # add only the pinned upstream helpers to the package search path.
            command = lxml_command(target, checkout)
            result['test_selection'] = [str(checkout / recipe['tests'])]
            stage('collect', [*command, '--list-tests'], checkout)
            if not (output / 'collect.log').read_text().strip():
                raise ValueError('lxml collected no tests')
            stage('test', command, checkout, timeout=1800)
        else:
            test_cwd = work if recipe.get('installed') else checkout
            if recipe.get('installed'):
                location = subprocess.check_output([str(target), '-c',
                    f'import {module}, pathlib; print(pathlib.Path({module}.__file__).parent)'], cwd=work,
                    env=env, text=True).strip()
                tests = Path(location) / recipe['tests']
            else:
                tests = checkout / recipe['tests']
            result['test_selection'] = [str(tests)]
            # Load upstream configuration/plugins but don't add source packages to sys.path.
            # NumPy's test directories may be namespace packages without __init__.py,
            # so --pyargs cannot locate them. Prepend mode on the installed path also
            # avoids re-importing native modules under a second name.
            import_mode = 'prepend' if recipe.get('installed') else recipe.get('import_mode', 'importlib')
            command = [str(target), '-P', '-m', 'pytest', f'--import-mode={import_mode}', '-o', 'pythonpath=', str(tests)]
            command.extend(recipe.get('pytest_args', []))
            stage('collect', [*command, '--collect-only', '-qq'], test_cwd, framework='pytest')
            stage('test', [*command, '--maxfail=10', f'--junitxml={output / "tests.xml"}'],
                  test_cwd, framework='pytest', timeout=1800)
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
    parser.add_argument('--build-only', action='store_true', help='Build a source wheel and resolve locks without running tests')
    parser.add_argument('--prepared-root', type=Path, help='Verified package build artifacts for this interpreter')
    parser.add_argument('--baseline-root', type=Path, help='Baseline locks used to constrain candidate dependency versions')
    args = parser.parse_args()
    result = run_project(args.project, args.commit, args.platform, args.mode, args.artifact, args.work, args.output,
                         build_only=args.build_only, prepared_root=args.prepared_root, baseline_root=args.baseline_root)
    print(json.dumps(result, indent=2))
    return 0 if result['status'] in ('passed', 'built', 'not-applicable') else 1


if __name__ == '__main__':
    raise SystemExit(main())

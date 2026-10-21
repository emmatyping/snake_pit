"""GitHub Actions matrices and native CPython build artifacts."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
from typing import TypedDict, cast

from .__main__ import load_platforms, plan
from .dependencies import load_projects
from .modes import DEFAULT_MODE, MODES, parse_mode
from .worker import sha256


class Target(TypedDict):
    platform: str
    runner: str


class TestTarget(Target):
    project: str


class Matrix(TypedDict):
    builds: list[Target]
    tests: list[TestTarget]
    projects: list[str]


class BuildManifest(TypedDict):
    commit: str
    platform: str
    mode: str
    source_path: str
    python: str
    python_sha256: str
    archive_sha256: str
    runner_image: str


RUNNERS = {
    'aarch64-apple-darwin': 'macos-26',
    'aarch64-unknown-linux-gnu': 'ubuntu-26.04-arm',
    'i686-pc-windows-msvc': 'windows-2025',
    'x86_64-pc-windows-msvc': 'windows-2025',
    'x86_64-unknown-linux-gnu': 'ubuntu-26.04',
}


def matrices(commit: str, platforms: list[str], mode: str) -> Matrix:
    config = parse_mode(mode)
    if not platforms or len(platforms) != len(set(platforms)):
        raise ValueError('provide a nonempty list of distinct platforms')
    builds: list[Target] = []
    tests: list[TestTarget] = []
    for platform in platforms:
        plan(commit, platform, mode)
        if config.freethreading and platform == 'i686-pc-windows-msvc':
            raise ValueError('CPython CI excludes free-threaded Windows x86 builds')
        builds.append({'platform': platform, 'runner': RUNNERS[platform]})
        # Keep unsupported combinations as explicit not-applicable test results.
        tests.extend({'platform': platform, 'runner': RUNNERS[platform], 'project': p['name']}
                     for p in load_projects())
    return {'builds': builds, 'tests': tests, 'projects': [p['name'] for p in load_projects()]}


def write_outputs(value: Matrix, path: Path) -> None:
    with path.open('a') as stream:
        stream.write(f'builds={json.dumps({"include": value["builds"]})}\n')
        stream.write(f'projects={json.dumps(value["projects"])}\n')


def call(argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    print('+', subprocess.list2cmdline(argv), flush=True)
    subprocess.run(argv, cwd=cwd, env=env, check=True)


def interpreter(source: Path, platform: str, mode: str) -> Path:
    config = parse_mode(mode)
    if 'windows' in platform:
        arch = 'win32' if platform.startswith('i686') else 'amd64'
        if config.freethreading:
            arch += 't'
        return source / 'PCbuild' / arch / ('python_d.exe' if config.debug else 'python.exe')
    return source / 'install/bin/python3'


def dependencies(source: Path, platform: str) -> None:
    """Use the dependency manifests from the requested CPython revision."""
    if 'linux' in platform:
        call(['sudo', 'bash', str(source / '.github/workflows/posix-deps-apt.sh')], source)
    elif 'apple' in platform:
        call(['brew', 'bundle', '--file=Misc/Brewfile'], source)
        call(['brew', 'install', 'make'], source)


def build(source: Path, output: Path, commit: str, platform: str, mode: str) -> None:
    matrices(commit, [platform], mode)
    config = parse_mode(mode)
    source = source.resolve()
    output = output.resolve()
    actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    if actual != commit:
        raise ValueError('CPython checkout does not match requested commit')
    dependencies(source, platform)
    env = dict(os.environ, PYTHONSTRICTEXTENSIONBUILD='1')
    if 'windows' in platform:
        arch = 'Win32' if platform.startswith('i686') else 'x64'
        argv = [os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/c',
                str(source / 'PCbuild/build.bat'), '-e', '-v', '-p', arch]
        argv.extend(['-d'] if config.debug else ['-c', 'Release'])
        if config.freethreading:
            argv.append('--disable-gil')
        if config.assertions and not config.debug:
            props = source / 'PCbuild/pyproject.props'
            if '<_DebugPreprocessorDefinition>NDEBUG;</_DebugPreprocessorDefinition>' not in props.read_text():
                raise ValueError('unrecognized PCbuild NDEBUG configuration; cannot enable release assertions')
            # A global MSBuild property overrides the release NDEBUG definition,
            # retaining Release optimization, CRT and ABI. A response file avoids
            # build.bat's special quoting requirements for arguments containing '='.
            response = source / 'PCbuild/snake-pit-assertions.rsp'
            response.write_text('/p:_DebugPreprocessorDefinition=\n')
            argv.append('@PCbuild/snake-pit-assertions.rsp')
        call(argv, source, env)
    else:
        flags = ['./configure', '--config-cache', f'--prefix={source / "install"}', '--with-ensurepip=no']
        if config.debug:
            flags.append('--with-pydebug')
        else:
            flags.extend(['--without-pydebug', '--with-assertions' if config.assertions else '--without-assertions'])
        if config.freethreading:
            flags.append('--disable-gil')
        # These flags are present in current CI but not in older commits.
        configure = (source / 'configure').read_text()
        if config.debug:
            flags.extend(flag for flag in ('--enable-safety', '--enable-slower-safety') if flag in configure)
        if 'apple' in platform:
            brewfile = (source / 'Misc/Brewfile').read_text()
            formula = re.search(r'brew [\"\'](openssl@[^\"\']+)', brewfile)
            if formula is None:
                raise ValueError('no versioned OpenSSL formula in CPython Brewfile')
            openssl = subprocess.check_output(['brew', '--prefix', formula[1]], text=True).strip()
            gdbm = subprocess.check_output(['brew', '--prefix', 'gdbm'], text=True).strip()
            env.update(GDBM_CFLAGS=f'-I{gdbm}/include', GDBM_LIBS=f'-L{gdbm}/lib -lgdbm')
            flags.append(f'--with-openssl={openssl}')
        else:
            env['CC'] = 'gcc'
        call(flags, source, env)
        call(['gmake' if 'apple' in platform else 'make', f'-j{os.cpu_count() or 2}'], source, env)
        # Meson and other extension builders require installed headers and sysconfig
        # paths; an in-tree interpreter alone does not provide that layout.
        call(['gmake' if 'apple' in platform else 'make', 'install', 'ENSUREPIP=no'], source, env)
    python = interpreter(source, platform, mode)
    output.mkdir(parents=True, exist_ok=True)
    with (output / 'pythoninfo.txt').open('w') as stream:
        subprocess.run([str(python), '-m', 'test.pythoninfo'], cwd=source, stdout=stream, check=True)
    # Keep the complete in-tree build: headers, generated sysconfig, DLLs and test helpers.
    # tar preserves executable modes and symlinks unlike a direct Actions artifact upload.
    archive = output / 'cpython.tar.gz'

    def include(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        return None if '.git' in Path(info.name).parts else info

    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(source, arcname='cpython', filter=include)
    manifest: BuildManifest = dict(commit=commit, platform=platform, mode=mode,
                                   source_path=str(source), python=str(python.relative_to(source)),
                                   python_sha256=sha256(python), archive_sha256=sha256(archive),
                                   runner_image=f"{os.environ.get('ImageOS', '')}-{os.environ.get('ImageVersion', '')}")
    (output / 'build.json').write_text(json.dumps(manifest, indent=2) + '\n')


def restore(artifact: Path, source: Path, commit: str, platform: str, mode: str) -> Path:
    manifest = cast(BuildManifest, json.loads((artifact / 'build.json').read_text()))
    if (manifest['commit'], manifest['platform'], manifest['mode']) != (commit, platform, mode):
        raise ValueError('CPython artifact provenance does not match the requested run')
    if str(source.resolve()) != manifest['source_path']:
        raise ValueError('restore the in-tree CPython build at its original absolute path for sysconfig')
    archive = artifact / 'cpython.tar.gz'
    if sha256(archive) != manifest['archive_sha256']:
        raise ValueError('CPython archive digest mismatch')
    with tarfile.open(archive) as tar:
        tar.extractall(source.parent, filter='data')
    python = source / manifest['python']
    if sha256(python) != manifest['python_sha256']:
        raise ValueError('restored interpreter digest mismatch')
    return python


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    matrix = sub.add_parser('matrix')
    matrix.add_argument('--commit', required=True)
    matrix.add_argument('--platform', nargs='+', choices=tuple(load_platforms()['platforms']), required=True)
    matrix.add_argument('--mode', choices=MODES, default=DEFAULT_MODE)
    matrix.add_argument('--github-output', type=Path)
    building = sub.add_parser('build')
    building.add_argument('--source', type=Path, default=Path('cpython'))
    building.add_argument('--output', type=Path, default=Path('cpython-artifact'))
    building.add_argument('--commit', required=True)
    building.add_argument('--platform', choices=tuple(load_platforms()['platforms']), required=True)
    building.add_argument('--mode', choices=MODES, default=DEFAULT_MODE)
    args = parser.parse_args()
    try:
        if args.command == 'matrix':
            value = matrices(args.commit, args.platform, args.mode)
            if args.github_output:
                write_outputs(value, args.github_output)
            else:
                print(json.dumps(value, indent=2))
        else:
            build(args.source, args.output, args.commit, args.platform, args.mode)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()

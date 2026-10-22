"""Read locked package identities and verified source-built wheels."""
import json
from email.parser import Parser
from pathlib import Path
import re
import tomllib
import zipfile
from typing import cast

from .models import ProjectResult
from .worker import sha256


def normalize(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


def locked_versions(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    lock = tomllib.loads(path.read_text())
    if lock.get('lock-version') != '1.0':
        raise ValueError(f'unsupported package lock: {path}')
    return {normalize(p['name']): str(p['version']) for p in lock.get('packages', []) if 'version' in p}


def read_results(root: Path, commit: str, platform: str, mode: str) -> dict[str, tuple[Path, ProjectResult]]:
    results: dict[str, tuple[Path, ProjectResult]] = {}
    for path in sorted(root.rglob('result.json')):
        result = cast(ProjectResult, json.loads(path.read_text()))
        if (result['cpython_commit'], result['platform'], result['mode']) != (commit, platform, mode):
            raise ValueError(f'package artifact provenance mismatch: {path}')
        name = result['project']
        if name in results:
            raise ValueError(f'duplicate package artifact: {name}')
        results[name] = (path.parent, result)
    return results


def verified_wheel(folder: Path, result: ProjectResult) -> Path:
    wheels = list((folder / 'wheels').glob('*.whl'))
    if len(wheels) != 1:
        raise ValueError(f'expected one prepared wheel for {result["project"]}')
    wheel = wheels[0]
    if sha256(wheel) != result['artifacts'].get(wheel.name):
        raise ValueError(f'prepared wheel digest mismatch: {wheel.name}')
    with zipfile.ZipFile(wheel) as archive:
        metadata = [info for info in archive.infolist() if info.filename.endswith('.dist-info/METADATA')]
        if len(metadata) != 1 or metadata[0].file_size > 1024 * 1024:
            raise ValueError('invalid prepared wheel metadata')
        message = Parser().parsestr(archive.read(metadata[0]).decode('utf-8'))
    if normalize(message.get('Name', '')) != normalize(result['project']) or message.get('Version') != result['release']:
        raise ValueError('prepared wheel package identity mismatch')
    return wheel.resolve()


def wheel_requirements(results: dict[str, tuple[Path, ProjectResult]], versions: dict[str, str],
                       project: str, recipe_hash: str) -> list[str]:
    requirements: list[str] = []
    for name, (folder, result) in sorted(results.items()):
        if name == project or result.get('build_status') != 'passed':
            continue
        if versions.get(normalize(name)) != result['release']:
            continue
        if result.get('recipe_sha256') != recipe_hash:
            raise ValueError(f'prepared wheel recipe mismatch: {name}')
        requirements.append(verified_wheel(folder, result).as_uri())
    return requirements

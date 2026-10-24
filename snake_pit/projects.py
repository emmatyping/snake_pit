"""Manage pinned project recipes without executing package code."""
import difflib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from string import Formatter
import subprocess
import tempfile
import tomllib
from typing import cast
from urllib.parse import quote, unquote, urlsplit
import urllib.request

from .artifacts import normalize
from .dependencies import RECIPES
from .models import Recipe

LIST_FIELDS = ('dependencies', 'build_dependencies', 'linux', 'macos', 'windows', 'selection', 'pytest_args', 'exclude_platforms')


def package_name(value: str) -> str:
    name = normalize(value)
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name):
        raise ValueError('provide a PyPI package name without extras or a version specifier')
    return name


def repository_url(value: str) -> str:
    url = urlsplit(value)
    if (url.scheme != 'https' or not url.hostname or not url.path.strip('/') or
            url.username is not None or url.password is not None or url.query or url.fragment):
        raise ValueError('repository must be a public HTTPS Git repository URL')
    return value.rstrip('/').removesuffix('.git')


def release(name: str, version: str | None = None) -> dict[str, str]:
    suffix = '' if version is None else quote(version, safe='') + '/'
    url = f'https://pypi.org/pypi/{quote(name, safe="")}/{suffix}json'
    request = urllib.request.Request(url, headers={'User-Agent': 'snake-pit', 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.load(response)
    if package_name(data['info']['name']) != name:
        raise ValueError('PyPI returned a different project')
    resolved_version = data['info']['version']
    if not isinstance(resolved_version, str) or not resolved_version:
        raise ValueError('PyPI returned an invalid version')
    sources = [file for file in data['urls'] if file['packagetype'] == 'sdist' and not file.get('yanked', False)]
    if len(sources) != 1:
        raise ValueError(f'{name} requires exactly one non-yanked source distribution')
    source = sources[0]
    parsed = urlsplit(source['url'])
    filename = unquote(parsed.path.rsplit('/', 1)[-1])
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None or
            parsed.password is not None or parsed.query or parsed.fragment or
            filename != source['filename'] or '/' in filename or '\\' in filename or
            not filename.endswith(('.tar.gz', '.tar.bz2', '.tar.xz', '.tar', '.tgz'))):
        raise ValueError(f'{name} requires an HTTPS tar source distribution supported by the worker')
    digest = source['digests']['sha256']
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError(f'{name} requires a SHA256 source digest')
    return {'version': resolved_version, 'sdist_url': source['url'], 'sdist_sha256': digest}


def format_tag(template: str, version: str) -> str:
    for _, field, spec, conversion in Formatter().parse(template):
        if field is not None and (field not in ('version', 'version_underscores') or spec or conversion):
            raise ValueError('tag placeholders must be {version} or {version_underscores}')
    return template.format(version=version, version_underscores=version.replace('.', '_'))


def tag_commit(repository: str, tag: str) -> str:
    repository = repository_url(repository)
    ref = f'refs/tags/{tag}'
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    try:
        subprocess.run(['git', 'check-ref-format', ref], check=True, capture_output=True, env=env)
        result = subprocess.run(['git', 'ls-remote', '--exit-code', '--tags', '--', repository + '.git', ref, ref + '^{}'],
                                check=True, capture_output=True, text=True, timeout=60, env=env)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f'cannot resolve release tag {tag!r} in {repository}') from exc
    refs = dict((name, commit) for commit, name in (line.split() for line in result.stdout.splitlines()))
    commit = refs.get(ref + '^{}', refs.get(ref, ''))
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError(f'release tag {tag!r} did not resolve to a full commit SHA')
    return commit


def validate_recipe(name: str, value: dict[str, object]) -> Recipe:
    unknown = value.keys() - Recipe.__annotations__.keys()
    if unknown:
        raise ValueError(f'{name}: unknown recipe fields: {", ".join(sorted(unknown))}')
    for key in ('module', 'tests'):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValueError(f'{name}: {key} is required')
    module = cast(str, value['module'])
    tests = cast(str, value['tests'])
    if not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*', module, flags=re.ASCII):
        raise ValueError(f'{name}: module must be a Python import name')
    if PurePosixPath(tests).is_absolute() or '..' in PurePosixPath(tests).parts or '\\' in tests or ':' in tests:
        raise ValueError(f'{name}: tests must be a relative path using forward slashes')
    for key in LIST_FIELDS:
        if key in value:
            items = value[key]
            if not isinstance(items, list) or any(not isinstance(item, str) or not item for item in items):
                raise ValueError(f'{name}: {key} must be a list of nonempty strings')
    if 'dependencies' not in value:
        raise ValueError(f'{name}: dependencies are required (use an empty list if none)')
    if 'installed' in value and not isinstance(value['installed'], bool):
        raise ValueError(f'{name}: installed must be a boolean')
    for key, choices in (('source', ('git', 'sdist')), ('adapter', ('django', 'lxml', 'cython')),
                         ('import_mode', ('importlib', 'prepend', 'append'))):
        if key in value and value[key] not in choices:
            raise ValueError(f'{name}: invalid {key}')
    if 'test_env' in value:
        env = value['test_env']
        if not isinstance(env, dict) or any(not isinstance(k, str) or not re.fullmatch(r'[A-Za-z_]\w*', k, re.ASCII)
                                          or not isinstance(v, str) for k, v in env.items()):
            raise ValueError(f'{name}: test_env must map environment variable names to strings')
    repository = value.get('repository', '')
    tag = value.get('tag', '')
    if not isinstance(repository, str) or not isinstance(tag, str):
        raise ValueError(f'{name}: repository and tag must be strings')
    if repository:
        repository_url(repository)
    if value.get('source') != 'sdist':
        if not repository or not tag:
            raise ValueError(f'{name}: Git test sources require --repository and --tag')
        format_tag(tag, '0')
    return cast(Recipe, value)


def toml_value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, list):
        return '[' + ', '.join(toml_value(item) for item in value) + ']'
    if isinstance(value, dict):
        return '{ ' + ', '.join(f'{toml_value(k)} = {toml_value(v)}' for k, v in value.items()) + ' }'
    raise ValueError(f'unsupported TOML value: {value!r}')


def update_table(text: str, name: str, updates: dict[str, object]) -> str:
    headers = list(re.finditer(r'(?m)^[ \t]*\[(?!\[)[^\n]+?\][ \t]*(?:#[^\n]*)?$', text))
    for index, header in enumerate(headers):
        if tomllib.loads(header.group()) != {name: {}}:
            continue
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        block = text[header.end():end]
        old = tomllib.loads(text)[name]
        for key, value in updates.items():
            # Only pinned scalar fields are replaced; preserve comments and all
            # other recipe text. Refuse unfamiliar syntax rather than corrupt it.
            pattern = rf'''(?m)^([ \t]*{re.escape(key)}[ \t]*=[ \t]*)("(?:[^"\\\n]|\\.)*"|'[^'\n]*')([ \t]*(?:\#[^\n]*)?)$'''
            block, count = re.subn(pattern, lambda m: m[1] + toml_value(value) + m[3], block)
            if count != 1:
                if count or key in old:
                    raise ValueError(f'{name}: cannot safely replace {key}; use a single-line string')
                block = block.rstrip('\n') + f'\n{key} = {toml_value(value)}\n\n'
        return text[:header.end()] + block + text[end:]
    raise ValueError(f'cannot find the top-level recipe table for {name}')


def save(path: Path, original: bytes | None, text: str, expected: dict[str, Recipe], dry_run: bool) -> str:
    if tomllib.loads(text) != expected:
        raise ValueError('recipe edit did not preserve the expected TOML document')
    before = '' if original is None else original.decode('utf-8').replace('\r\n', '\n')
    diff = ''.join(difflib.unified_diff(before.splitlines(keepends=True), text.splitlines(keepends=True),
                                        fromfile=str(path), tofile=str(path)))
    if dry_run or not diff:
        return diff
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        if (path.read_bytes() if path.exists() else None) != original:
            raise ValueError('recipes changed during the update; rerun with the current file')
        temporary.chmod(stat.S_IMODE(path.stat().st_mode) if original is not None else 0o644)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return diff


def add_project(name: str, configuration: dict[str, object], *, version: str | None = None,
                commit: str | None = None, path: Path = RECIPES, dry_run: bool = False) -> str:
    name = package_name(name)
    path = path.resolve()
    original = path.read_bytes() if path.exists() else None
    text = '' if original is None else original.decode('utf-8').replace('\r\n', '\n')
    recipes = cast(dict[str, Recipe], tomllib.loads(text))
    if name in recipes:
        raise ValueError(f'{name} already exists; use update')
    recipe = validate_recipe(name, configuration.copy())
    if recipe.get('source') == 'sdist' and commit is not None:
        raise ValueError('--commit only applies to Git test sources')
    metadata = release(name, version)
    recipe.update(cast(Recipe, metadata))
    if recipe.get('source') != 'sdist':
        recipe['repository'] = repository_url(recipe['repository'])
        resolved = tag_commit(recipe['repository'], format_tag(recipe['tag'], recipe['version']))
        if commit is not None and commit != resolved:
            raise ValueError('--commit does not match the resolved release tag')
        recipe['commit'] = resolved
    recipes[name] = recipe
    text = text.rstrip('\n') + ('\n\n' if text else '') + f'[{name}]\n'
    text += ''.join(f'{key} = {toml_value(value)}\n' for key, value in recipe.items())
    return save(path, original, text, recipes, dry_run)


def update_projects(name: str | None = None, *, version: str | None = None, tag: str | None = None,
                    path: Path = RECIPES, dry_run: bool = False) -> str:
    if name is None and (version is not None or tag is not None):
        raise ValueError('--version and --tag require a particular package')
    path = path.resolve()
    original = path.read_bytes()
    text = original.decode('utf-8').replace('\r\n', '\n')
    recipes = cast(dict[str, Recipe], tomllib.loads(text))
    if name is not None:
        name = package_name(name)
        if name not in recipes:
            raise ValueError(f'{name} is not in recipes.toml; use add')
    for project in ([name] if name is not None else list(recipes)):
        try:
            recipe = recipes[project]
            validate_recipe(project, cast(dict[str, object], recipe))
            metadata = release(project, version)
            if metadata['version'] == recipe['version'] and tag is None:
                continue  # Keep existing immutable pins when there is no new release.
            updates: dict[str, object] = dict(metadata)
            if recipe.get('source') != 'sdist':
                template = recipe['tag'] if tag is None else tag
                if not any(field in ('version', 'version_underscores')
                           for _, field, _, _ in Formatter().parse(template)) and tag is None:
                    raise ValueError('a fixed release tag needs an explicit --tag override for a new version')
                updates['commit'] = tag_commit(recipe['repository'], format_tag(template, metadata['version']))
                if tag is not None:
                    updates['tag'] = tag
            elif tag is not None:
                raise ValueError('--tag only applies to Git test sources')
            text = update_table(text, project, updates)
            recipes[project] = cast(Recipe, {**recipe, **updates})
        except (ValueError, OSError, KeyError) as exc:
            raise ValueError(f'{project}: {exc}') from exc
    return save(path, original, text, recipes, dry_run)

"""Resolve public GitHub CPython branches, commits and PR heads once per comparison."""
import json
import os
import re
from typing import TypedDict, cast
from urllib.parse import quote, unquote, urlsplit
import urllib.request


class Revision(TypedDict):
    repository: str
    ref: str
    commit: str


def github_json(path: str) -> dict[str, object]:
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'snake-pit'}
    token = os.environ.get('GITHUB_TOKEN')
    if token:
        headers['Authorization'] = f'Bearer {token}'
    request = urllib.request.Request('https://api.github.com/' + path, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return cast(dict[str, object], json.load(response))


def parse_ref(value: str) -> tuple[str, str, int | None]:
    repository, ref, pull = 'python/cpython', value, None
    if value.startswith('https://'):
        url = urlsplit(value)
        if url.netloc != 'github.com' or url.query or url.fragment:
            raise ValueError('use a github.com branch, commit or pull-request URL')
        parts = unquote(url.path).strip('/').split('/')
        if len(parts) < 4 or parts[2] not in ('tree', 'commit', 'pull'):
            raise ValueError('expected a GitHub tree, commit or pull URL')
        repository, ref = '/'.join(parts[:2]), '/'.join(parts[3:])
        if parts[2] == 'pull':
            if len(parts) != 4 or not ref.isdigit():
                raise ValueError('expected a numeric pull-request URL')
            pull = int(ref)
    elif ':' in value:
        repository, ref = value.split(':', 1)
    match = re.fullmatch(r'refs/pull/(\d+)/head', ref)
    if match:
        pull = int(match[1])
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('repository must be owner/name on GitHub')
    if not ref or any(c.isspace() for c in ref) or any(c in ref for c in '?*[]~^\\'):
        raise ValueError('invalid GitHub ref')
    return repository, ref, pull


def resolve(value: str) -> Revision:
    repository, ref, pull = parse_ref(value)
    if pull is not None:
        data = github_json(f'repos/{repository}/pulls/{pull}')
        head = cast(dict[str, object], data['head'])
        head_repo = cast(dict[str, object] | None, head.get('repo'))
        if head_repo is not None:
            repository = str(head_repo['full_name'])
        commit = str(head['sha'])
    else:
        data = github_json(f'repos/{repository}/commits/{quote(ref, safe="")}')
        commit = str(data['sha'])
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('GitHub did not resolve the ref to a full commit SHA')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('invalid GitHub head repository')
    return dict(repository=repository, ref=value, commit=commit)

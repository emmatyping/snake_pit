"""Read project versions and verified source artifacts from executable recipes."""
from pathlib import Path
import re
import tomllib
from typing import cast
from urllib.parse import unquote, urlsplit

from .models import Project, Recipe

ROOT = Path(__file__).resolve().parents[1]
RECIPES = ROOT / 'data/recipes.toml'


def load_recipes(path: Path = RECIPES) -> dict[str, Recipe]:
    return cast(dict[str, Recipe], tomllib.loads(path.read_text()))


def load_projects(path: Path = RECIPES) -> list[Project]:
    projects: list[Project] = []
    for name, recipe in load_recipes(path).items():
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name):
            raise ValueError(f'project name must be normalized: {name}')
        if not recipe['version']:
            raise ValueError(f'{name} requires a version')
        source = urlsplit(recipe['sdist_url'])
        filename = unquote(source.path.rsplit('/', 1)[-1])
        if source.scheme != 'https' or not source.netloc:
            raise ValueError(f'{name} requires an HTTPS source archive URL')
        if not filename or Path(filename).name != filename or filename in ('.', '..'):
            raise ValueError(f'invalid source archive filename for {name}')
        digest = recipe['sdist_sha256']
        if not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError(f'{name} requires a SHA256 source digest')
        projects.append(dict(name=name, release=recipe['version'],
                             sdist=dict(filename=filename, url=recipe['sdist_url'], sha256=digest)))
    return projects

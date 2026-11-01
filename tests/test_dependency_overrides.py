import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from snake_pit.artifacts import override_requirements
from snake_pit.dependencies import load_recipes
from snake_pit.models import Project, ProjectResult


class OverrideTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('uv'), 'uv is required for isolated build integration')
    def test_isolated_build_uses_constrained_build_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / 'fixture_build_helper-2.0-py3-none-any.whl'
            with zipfile.ZipFile(helper, 'w') as archive:
                info = 'fixture_build_helper-2.0.dist-info/'
                archive.writestr(info + 'METADATA', 'Metadata-Version: 2.3\nName: fixture-build-helper\nVersion: 2.0\n')
                archive.writestr(info + 'WHEEL', 'Wheel-Version: 1.0\nTag: py3-none-any\n')
                archive.writestr(info + 'RECORD', '')
                archive.writestr('fixture_build_helper.py', 'VALUE = "pinned isolated dependency"\n')
            constraints = root / 'build-constraints.txt'
            constraints.write_text(f'fixture-build-helper @ {helper.as_uri()}\n')
            source = root / 'source'
            source.mkdir()
            (source / 'pyproject.toml').write_text(
                '[build-system]\nrequires=["fixture-build-helper>=1"]\n'
                'build-backend="backend"\nbackend-path=["."]\n'
                '[project]\nname="isolation-fixture"\nversion="1.0"\n')
            (source / 'backend.py').write_text('''from pathlib import Path
import zipfile
from fixture_build_helper import VALUE

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    assert VALUE == "pinned isolated dependency"
    name = "isolation_fixture-1.0-py3-none-any.whl"
    with zipfile.ZipFile(Path(wheel_directory) / name, "w") as archive:
        info = "isolation_fixture-1.0.dist-info/"
        archive.writestr(info + "METADATA", "Metadata-Version: 2.3\\nName: isolation-fixture\\nVersion: 1.0\\n")
        archive.writestr(info + "WHEEL", "Wheel-Version: 1.0\\nTag: py3-none-any\\n")
        archive.writestr(info + "RECORD", "")
    return name
''')
            env = dict(os.environ, UV_BUILD_CONSTRAINT=str(constraints))
            env.pop('UV_NO_BUILD_ISOLATION', None)
            subprocess.run(['uv', 'build', '--wheel', '--python', sys.executable,
                            '--no-index', '--offline', '--no-config', '--no-python-downloads',
                            '--cache-dir', str(root / 'cache'), '--out-dir', str(root / 'dist'), str(source)],
                           env=env, check=True, capture_output=True, text=True)
            self.assertEqual(len(list((root / 'dist').glob('*.whl'))), 1)

    def test_catalog_overrides_cffi_for_every_consumer_but_not_itself(self) -> None:
        for name, recipe in load_recipes().items():
            with self.subTest(project=name):
                self.assertEqual('cffi' in recipe.get('dependency_overrides', []), name != 'cffi')

    def project(self) -> Project:
        return dict(name='cffi', release='2.2.0.dev0',
                    sdist=dict(filename='commit.tar.gz', url='https://example.invalid/commit.tar.gz',
                               sha256='a' * 64))

    def test_source_override_retains_hash_without_prepared_wheel(self) -> None:
        self.assertEqual(override_requirements(['cffi'], [self.project()], {}, 'recipe'),
                         ['cffi @ https://example.invalid/commit.tar.gz#sha256=' + 'a' * 64])
        with self.assertRaisesRegex(ValueError, 'unknown dependency override'):
            override_requirements(['missing'], [self.project()], {}, 'recipe')

    def test_prepared_override_checks_recipe_and_version(self) -> None:
        result: ProjectResult = dict(project='cffi', release='2.2.0.dev0', cpython_commit='commit',
                                     platform='platform', mode='release', status='built',
                                     build_status='passed', recipe_sha256='recipe', phases=[], artifacts={}, started_at='')
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / 'cffi.whl'
            prepared = {'cffi': (Path(directory), result)}
            with patch('snake_pit.artifacts.verified_wheel', return_value=wheel) as verify:
                self.assertEqual(override_requirements(['cffi'], [self.project()], prepared, 'recipe'),
                                 [f'cffi @ {wheel.as_uri()}'])
                verify.assert_called_once()
            for field in ('recipe_sha256', 'release'):
                invalid = result.copy()
                invalid[field] = 'wrong'
                with self.assertRaisesRegex(ValueError, 'prepared dependency override mismatch'):
                    override_requirements(['cffi'], [self.project()], {'cffi': (Path(directory), invalid)}, 'recipe')

    @unittest.skipUnless(shutil.which('uv'), 'uv is required for resolver integration')
    def test_resolver_overrides_transitive_pin_without_adding_unused_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def wheel(name: str, version: str, dependency: str = '') -> Path:
                path = root / f'{name}-{version}-py3-none-any.whl'
                with zipfile.ZipFile(path, 'w') as archive:
                    info = f'{name}-{version}.dist-info/'
                    metadata = f'Metadata-Version: 2.3\nName: {name}\nVersion: {version}\n'
                    if dependency:
                        metadata += f'Requires-Dist: {dependency}\n'
                    archive.writestr(info + 'METADATA', metadata)
                    archive.writestr(info + 'WHEEL', 'Wheel-Version: 1.0\nTag: py3-none-any\n')
                    archive.writestr(info + 'RECORD', '')
                return path

            fixed = wheel('cffi', '2.2.0.dev0')
            consumer = wheel('consumer', '1.0', 'cffi==2.1.1')
            unrelated = wheel('unrelated', '1.0')
            overrides = root / 'overrides.txt'
            overrides.write_text(f'cffi @ {fixed.as_uri()}#sha256={hashlib.sha256(fixed.read_bytes()).hexdigest()}\n')
            for target, expected in ((consumer, True), (unrelated, False)):
                inputs = root / 'requirements.in'
                inputs.write_text(target.as_uri() + '\n')
                lock = root / 'requirements.txt'
                subprocess.run(['uv', 'pip', 'compile', str(inputs), '--override', str(overrides),
                                '--no-index', '--no-config', '--no-python-downloads', '--python', sys.executable,
                                '--cache-dir', str(root / 'cache'), '-o', str(lock)],
                               check=True, capture_output=True, text=True)
                contents = lock.read_text()
                self.assertEqual('\ncffi @ ' in contents, expected)
                self.assertNotIn('cffi==2.1.1', contents)

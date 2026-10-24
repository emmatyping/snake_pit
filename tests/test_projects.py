import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import call, patch

from snake_pit.__main__ import main
from snake_pit.dependencies import RECIPES, load_projects, load_recipes
from snake_pit.projects import add_project, format_tag, package_name, release, tag_commit, update_projects


DOCUMENT = '''# Keep this recipe and its comments.
[demo]
version = "1.0" # release pin
sdist_url = "https://files.pythonhosted.org/demo-1.0.tar.gz"
sdist_sha256 = "''' + 'a' * 64 + '''"
repository = "https://github.com/example/demo"
tag = "v{version}"
commit = "''' + 'b' * 40 + '''"
module = "demo"
tests = "tests"
dependencies = [
    "pytest", # Keep this test dependency comment.
    "demo-vectors=={version}",
]
test_env = { FLAG = "" }
'''


def metadata(name: str = 'demo', version: str = '2.0') -> dict[str, str]:
    return dict(version=version, sdist_url=f'https://files.pythonhosted.org/{name}-{version}.tar.gz',
                sdist_sha256='c' * 64)


def configuration() -> dict[str, object]:
    return dict(repository='https://github.com/example/demo.git', tag='v{version}', module='demo',
                tests='tests', dependencies=['pytest'])


def pypi_response() -> dict[str, object]:
    return {'info': {'name': 'Demo', 'version': '2.0'}, 'urls': [
        {'packagetype': 'sdist', 'filename': 'demo-2.0.tar.gz', 'yanked': False,
         'url': 'https://files.pythonhosted.org/demo-2.0.tar.gz', 'digests': {'sha256': 'c' * 64}}]}


class ReleaseTests(unittest.TestCase):
    def test_release_uses_pypi_version_and_artifact_identity(self) -> None:
        with patch('snake_pit.projects.urllib.request.urlopen', return_value=io.BytesIO(json.dumps(pypi_response()).encode())) as api:
            self.assertEqual(release('demo'), metadata())
        self.assertEqual(api.call_args.args[0].full_url, 'https://pypi.org/pypi/demo/json')
        with patch('snake_pit.projects.urllib.request.urlopen', return_value=io.BytesIO(json.dumps(pypi_response()).encode())) as api:
            release('demo', '2.0')
        self.assertEqual(api.call_args.args[0].full_url, 'https://pypi.org/pypi/demo/2.0/json')

    def test_unusable_or_mismatched_sources_are_rejected(self) -> None:
        for change in ({'yanked': True}, {'filename': 'demo.zip'}, {'digests': {'sha256': 'invalid'}},
                       {'url': 'http://files.pythonhosted.org/demo-2.0.tar.gz'}, {'packagetype': 'bdist_wheel'}):
            data = pypi_response()
            sources = data['urls']
            assert isinstance(sources, list)
            sources[0].update(change)
            with self.subTest(change=change), \
                    patch('snake_pit.projects.urllib.request.urlopen', return_value=io.BytesIO(json.dumps(data).encode())), \
                    self.assertRaises(ValueError):
                release('demo')
        with patch('snake_pit.projects.urllib.request.urlopen', return_value=io.BytesIO(json.dumps(pypi_response()).encode())), \
                self.assertRaisesRegex(ValueError, 'different project'):
            release('other')

    def test_package_normalization_and_tag_placeholders(self) -> None:
        self.assertEqual(package_name('My_Package.Name'), 'my-package-name')
        self.assertEqual(format_tag('rel_{version_underscores}', '2.1.0'), 'rel_2_1_0')
        for name in ('demo[extra]', 'demo==1', '../demo'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                package_name(name)
        for template in ('{unknown}', '{version.__class__}', '{version!r}', '{version:10}'):
            with self.subTest(template=template), self.assertRaises(ValueError):
                format_tag(template, '2.0')

    def test_annotated_tag_uses_peeled_commit(self) -> None:
        output = 'a' * 40 + '\trefs/tags/v2.0\n' + 'b' * 40 + '\trefs/tags/v2.0^{}\n'
        with patch('snake_pit.projects.subprocess.run', return_value=subprocess.CompletedProcess([], 0, output)) as run:
            self.assertEqual(tag_commit('https://github.com/example/demo.git', 'v2.0'), 'b' * 40)
        self.assertEqual(run.call_args.args[0][-3:],
                         ['https://github.com/example/demo.git', 'refs/tags/v2.0', 'refs/tags/v2.0^{}'])
        with patch('snake_pit.projects.subprocess.run', side_effect=subprocess.CalledProcessError(2, 'git')), \
                self.assertRaisesRegex(ValueError, 'cannot resolve release tag'):
            tag_commit('https://github.com/example/demo', 'missing')


class ProjectManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'recipes.toml'
        self.path.write_bytes(DOCUMENT.encode())

    def test_single_update_keeps_comments_and_test_configuration(self) -> None:
        before = load_recipes(self.path)['demo']
        with patch('snake_pit.projects.release', return_value=metadata()), \
                patch('snake_pit.projects.tag_commit', return_value='d' * 40) as tag:
            diff = update_projects('Demo', path=self.path)
        after = load_recipes(self.path)['demo']
        self.assertEqual(after, {**before, **metadata(), 'commit': 'd' * 40})
        self.assertIn('# release pin', self.path.read_text())
        self.assertIn('# Keep this test dependency comment.', self.path.read_text())
        self.assertIn('+version = "2.0" # release pin', diff)
        tag.assert_called_once_with('https://github.com/example/demo', 'v2.0')
        self.assertNotIn(b'\r\n', self.path.read_bytes())

    def test_all_catalog_recipes_can_be_updated_without_losing_custom_fields(self) -> None:
        self.path.write_bytes(RECIPES.read_bytes())
        before = load_recipes(self.path)
        with patch('snake_pit.projects.release', side_effect=lambda name, version: metadata(name, '999.0')), \
                patch('snake_pit.projects.tag_commit', return_value='d' * 40) as tag:
            update_projects(path=self.path)
        after = load_recipes(self.path)
        self.assertEqual(len(load_projects(self.path)), len(before))
        for name, old in before.items():
            self.assertEqual(after[name], {**old, **metadata(name, '999.0'),
                                          **({'commit': 'd' * 40} if old.get('source') != 'sdist' else {})})
        self.assertIn(call(before['sqlalchemy']['repository'], 'rel_999_0'), tag.call_args_list)
        self.assertEqual(tag.call_count, len(before) - 1)

    def test_batch_failure_and_dry_run_leave_the_file_untouched(self) -> None:
        self.path.write_bytes(RECIPES.read_bytes())
        before = self.path.read_bytes()
        with patch('snake_pit.projects.release', side_effect=[metadata('urllib3'), OSError('PyPI unavailable')]), \
                patch('snake_pit.projects.tag_commit', return_value='d' * 40), \
                self.assertRaisesRegex(ValueError, 'httpx: PyPI unavailable'):
            update_projects(path=self.path)
        self.assertEqual(self.path.read_bytes(), before)
        with patch('snake_pit.projects.release', return_value=metadata('urllib3', '999.0')), \
                patch('snake_pit.projects.tag_commit', return_value='d' * 40):
            diff = update_projects('urllib3', path=self.path, dry_run=True)
        self.assertIn('+version = "999.0"', diff)
        self.assertEqual(self.path.read_bytes(), before)

    def test_no_new_release_keeps_immutable_pins(self) -> None:
        before = self.path.read_bytes()
        with patch('snake_pit.projects.release', return_value=metadata(version='1.0')), \
                patch('snake_pit.projects.tag_commit') as tag:
            self.assertEqual(update_projects(path=self.path), '')
        tag.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_add_fetches_source_and_resolves_git_tag(self) -> None:
        self.path.unlink()
        with patch('snake_pit.projects.release', return_value=metadata('my-package')), \
                patch('snake_pit.projects.tag_commit', return_value='d' * 40):
            add_project('My_Package', configuration(), path=self.path)
        recipe = load_recipes(self.path)['my-package']
        self.assertEqual(recipe['repository'], 'https://github.com/example/demo')
        self.assertEqual(recipe['commit'], 'd' * 40)
        self.assertEqual(load_projects(self.path)[0]['release'], '2.0')

    def test_add_sdist_tests_needs_no_git_pin(self) -> None:
        with patch('snake_pit.projects.release', return_value=metadata('other')), \
                patch('snake_pit.projects.tag_commit') as tag:
            add_project('other', dict(source='sdist', module='other', tests='tests', dependencies=[]), path=self.path)
        tag.assert_not_called()
        self.assertNotIn('commit', load_recipes(self.path)['other'])

    def test_duplicate_unknown_and_invalid_recipes_fail_before_network_requests(self) -> None:
        with patch('snake_pit.projects.release') as api:
            with self.assertRaisesRegex(ValueError, 'already exists'):
                add_project('Demo', configuration(), path=self.path)
            with self.assertRaisesRegex(ValueError, 'not in recipes'):
                update_projects('other', path=self.path)
            with self.assertRaisesRegex(ValueError, 'repository and --tag'):
                add_project('other', dict(module='other', tests='tests', dependencies=[]), path=self.path)
            for key, value in (('module', 'os; run()'), ('tests', '../escape'), ('dependencies', 'pytest'),
                               ('test_env', {'BAD-NAME': '1'}), ('unknown', 'value')):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    add_project('other', {**configuration(), key: value}, path=self.path)
        api.assert_not_called()

    def test_mismatched_commit_and_missing_tag_do_not_write(self) -> None:
        before = self.path.read_bytes()
        with patch('snake_pit.projects.release', return_value=metadata('other')), \
                patch('snake_pit.projects.tag_commit', return_value='d' * 40), \
                self.assertRaisesRegex(ValueError, 'does not match'):
            add_project('other', configuration(), commit='e' * 40, path=self.path)
        with patch('snake_pit.projects.release', return_value=metadata()), \
                patch('snake_pit.projects.tag_commit', side_effect=ValueError('missing tag')), \
                self.assertRaisesRegex(ValueError, 'demo: missing tag'):
            update_projects('demo', path=self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_concurrent_edits_are_preserved(self) -> None:
        def edit(name: str, version: str | None) -> dict[str, str]:
            self.path.write_bytes(b'# An edit made while fetching metadata.\n' + DOCUMENT.encode())
            return metadata()
        with patch('snake_pit.projects.release', side_effect=edit), \
                patch('snake_pit.projects.tag_commit', return_value='d' * 40), \
                self.assertRaisesRegex(ValueError, 'recipes changed'):
            update_projects('demo', path=self.path)
        self.assertTrue(self.path.read_text().startswith('# An edit made'))
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_fixed_tag_requires_an_override_for_a_new_version(self) -> None:
        self.path.write_text(DOCUMENT.replace('v{version}', 'v1.0'))
        with patch('snake_pit.projects.release', return_value=metadata()), \
                patch('snake_pit.projects.tag_commit', return_value='d' * 40) as tag:
            with self.assertRaisesRegex(ValueError, 'fixed release tag'):
                update_projects('demo', path=self.path)
            update_projects('demo', tag='new-{version}', path=self.path)
        tag.assert_called_once_with('https://github.com/example/demo', 'new-2.0')
        self.assertEqual(load_recipes(self.path)['demo']['tag'], 'new-{version}')

    def test_cli_exposes_recipe_fields_and_update_all(self) -> None:
        argv = ['snake_pit', 'add', 'other', '--recipes', str(self.path), '--repository', 'https://github.com/example/other',
                '--tag', '{version}', '--module', 'other', '--tests', 'test', '--dependency', 'pytest',
                '--build-dependency', 'maturin', '--linux', 'libffi-dev', '--macos', 'libffi', '--windows', 'libffi',
                '--installed', '--import-mode', 'prepend', '--pytest-arg=-W', '--pytest-arg=default', '--test-env', 'FLAG=']
        with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()), \
                patch('snake_pit.projects.release', return_value=metadata('other')), \
                patch('snake_pit.projects.tag_commit', return_value='d' * 40):
            self.assertEqual(main(), 0)
        recipe = load_recipes(self.path)['other']
        self.assertEqual(recipe['test_env'], {'FLAG': ''})
        self.assertEqual(recipe['build_dependencies'], ['maturin'])
        self.assertEqual(recipe['pytest_args'], ['-W', 'default'])
        self.assertTrue(recipe['installed'])
        with patch.object(sys, 'argv', ['snake_pit', 'update']), contextlib.redirect_stdout(io.StringIO()), \
                patch('snake_pit.projects.update_projects', return_value='') as update:
            self.assertEqual(main(), 0)
        update.assert_called_once_with(None, version=None, tag=None, path=RECIPES, dry_run=False)

    def test_cli_supports_empty_test_dependencies_and_requires_package_for_version(self) -> None:
        with patch.object(sys, 'argv', ['snake_pit', 'add', 'other', '--source', 'sdist', '--module', 'other',
                                       '--no-test-dependencies', '--recipes', str(self.path)]), \
                contextlib.redirect_stdout(io.StringIO()), patch('snake_pit.projects.release', return_value=metadata('other')):
            self.assertEqual(main(), 0)
        self.assertEqual(load_recipes(self.path)['other']['dependencies'], [])
        with patch.object(sys, 'argv', ['snake_pit', 'update', '--version', '2.0', '--recipes', str(self.path)]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()

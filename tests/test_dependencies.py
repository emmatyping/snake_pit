from pathlib import Path
import tempfile
import unittest

from snake_pit.dependencies import load_projects


class DependencyTests(unittest.TestCase):
    def test_recipe_version_is_the_project_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'recipes.toml'
            template = ('[demo]\nversion="{version}"\n'
                        'sdist_url="https://example.invalid/demo-{version}.tar.gz"\n'
                        'sdist_sha256="' + 'a' * 64 + '"\n')
            path.write_text(template.format(version='9.1'))
            self.assertEqual(load_projects(path)[0]['release'], '9.1')
            path.write_text(template.format(version='9.2'))
            project = load_projects(path)[0]
            self.assertEqual(project['release'], '9.2')
            self.assertEqual(project['sdist']['filename'], 'demo-9.2.tar.gz')

    def test_invalid_recipe_source_hash_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'recipes.toml'
            path.write_text('[demo]\nversion="9.1"\n'
                            'sdist_url="https://example.invalid/demo-9.1.tar.gz"\n'
                            'sdist_sha256="invalid"\n')
            with self.assertRaisesRegex(ValueError, 'SHA256 source digest'):
                load_projects(path)


if __name__ == '__main__':
    unittest.main()

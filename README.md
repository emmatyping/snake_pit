# Snake Pit

Compare a CPython GitHub branch, commit or PR head against a baseline on the
selected Tier 1 platforms. The baseline defaults to the PR's base branch for PR
references, or `python/cpython`'s `main` otherwise. Project names, versions, source hashes,
and test commands live in [data/recipes.toml](data/recipes.toml).

The workflow resolves both refs to immutable SHAs, builds both interpreters and
prepares source wheels. It then skips projects whose locked dependencies have
known build failures and runs the remaining suites in parallel. A dependency's
test failures do not block its consumers. Matching source-built dependency wheels
are reused, and candidate dependency versions are constrained by the baseline.

The Actions summary shows new regressions, improvements, unchanged failures and
newly blocked projects, with machine-readable results in `comparison.json`.
The build mode defaults to `debug`; `release_assertions` and `release` are also
available. Append `-freethreading` to any mode to disable the GIL.
See [running in Actions](docs/actions.md) for reference forms, artifacts and limits.

Requires Python 3.14+ for the control tools. Runtime code uses the standard library;
mypy is a locked development dependency. The control Python and the CPython
interpreter under test are separate inputs.

```sh
uv python install 3.14
uv sync --locked
uv run --locked mypy
uv run --locked python -m unittest discover -s tests -v
uv run --locked python -m snake_pit list
uv run --locked python -m snake_pit plan --ref "$CPYTHON_REF" --platform "$TARGET_TRIPLE"
```

`CPYTHON_REF` can be a branch or commit in `python/cpython`, a GitHub PR/commit/tree
URL, or `owner/repo:branch` for a fork. `TARGET_TRIPLE` must be provided explicitly.
A full SHA still works with `--commit` for a low-level single-interpreter plan.

To dispatch a comparison:

```sh
gh workflow run downstream.yml -f ref="$CPYTHON_REF" \
  -f platforms="$TARGET_TRIPLE" -f mode=release
```

Override the baseline with `-f base_ref=3.15` (or `--base-ref 3.15` for local
planning). For intermittent checks of main itself, dispatch with `-f ref=main`.
When target and baseline resolve to the same commit, builds and tests run only
once and their outcomes are reused for both sides of the comparison.

The optional `projects` input narrows the selected catalog, for example
`-f projects=packaging` for a small workflow smoke check. By default every recipe
is selected. The recorded Tier 1 set, checked
against [PEP 11](https://peps.python.org/pep-0011/#tier-1) on October 21, 2026, is:

| Target | Toolchain |
| --- | --- |
| `aarch64-apple-darwin` | clang |
| `aarch64-unknown-linux-gnu` | GCC, glibc |
| `i686-pc-windows-msvc` | MSVC |
| `x86_64-pc-windows-msvc` | MSVC |
| `x86_64-unknown-linux-gnu` | GCC, glibc |

The policy snapshot is in [data/platforms.json](data/platforms.json). Tier status
can change; update it deliberately. OS versions, compiler versions, SDKs, and
system libraries must also be pinned in an actual worker image. Package support
is separate from CPython platform support; for example uvloop excludes Windows.

## Updating the project list

Manage [data/recipes.toml](data/recipes.toml) with the project commands:

```sh
uv run --locked python -m snake_pit update packaging  # One project
uv run --locked python -m snake_pit update            # All configured projects
uv run --locked python -m snake_pit update --dry-run  # Review changes without writing
```

Updates use the latest release reported by the [PyPI JSON API](https://docs.pypi.org/api/json/).
The version, source archive URL/hash and upstream release-tag commit are updated
together. Annotated Git tags are resolved to their underlying commit. Existing
test settings and comments are preserved. A missing source archive, unresolved tag
or failed request aborts the whole operation without writing partial updates.
Unchanged versions keep their existing immutable pins. Updating does not build,
install or execute downloaded package code.

Select a specific release with `update packaging --version 25.0`. Override a
project's tag template with `update numpy --tag 'v{version}'`; `--version` and
`--tag` require a particular package. Templates support `{version}` and
`{version_underscores}`. A fixed literal tag requires an explicit `--tag` override
when moving to another version. Tests sourced from the sdist need no Git pin.

To add a project, supply its import name and upstream test source:

```sh
uv run --locked python -m snake_pit add tomli-w \
  --repository https://github.com/hukkin/tomli-w \
  --tag '{version}' --module tomli_w --tests tests \
  --dependency pytest
```

`add` obtains the source URL, SHA-256 and version from PyPI and resolves the release
tag automatically. `--version` selects an exact release; optional `--commit` checks
an expected tag commit. Package names are normalized. For tests included in a
source archive, use `--source sdist`; repository and tag are then optional. The
worker currently requires a non-yanked tar sdist; wheel-only and zip-only releases
are rejected. Added recipes still need build/test qualification on the desired
CPython revisions and platforms.

`--dependency` and `--build-dependency` are repeatable. Test dependencies default to
`pytest` when none are supplied; `--no-test-dependencies` records an empty list.
Additional recipe settings are exposed through
`--installed`, `--adapter`, `--selection`, `--import-mode`, `--pytest-arg`,
`--test-env NAME=VALUE`, and repeatable `--linux`, `--macos`, and `--windows`
options. Use `--pytest-arg=-W --pytest-arg=default` for arguments beginning with
a dash, and `--test-env FLAG=` for an empty environment value. See `add --help`
for the complete options.

`--exclude-platform TARGET` records unsupported platforms (repeatable). Memray
and uvloop exclude the Windows targets. The `cython` adapter runs a selected
native compiler suite and merges its XML reports; SymPy selects its core suite,
Memray its unit suite, and Matplotlib installed, headless transformation tests.

Both commands support `--dry-run` and `--recipes PATH` for a different recipe file.
Successful writes are atomic and use LF line endings. Concurrent edits are detected
before replacement. The commands only edit local recipes; they do not commit or
push changes. Actions creates jobs only for recipe entries.

The development environment contains the control tools. Jobs generate their own
build and runtime/test `pylock.toml` files for the target interpreter; those locks
are uploaded with the results.

## Prepared workers

Only run the worker executor **inside an already isolated disposable worker**:

```sh
python3 -m snake_pit worker /bundle/job.json --output /results/run-001
```

External orchestration must provide filesystem and network isolation, enforce
resource limits, and always destroy the worker after a job. Do not
run downloaded build hooks or tests on a developer machine. See
[the job contract](docs/worker.md) before constructing a job.

The worker checks input digests, records provenance, executes ordered phases with
timeouts, bounds saved logs, distinguishes process crashes from ordinary failures,
and skips later phases after failure. Installer offline environment variables are
conveniences; network denial must be enforced outside the process. Arbitrary
recipes are trusted configuration and must be reviewed.

Recipe versions and source hashes identify releases and artifacts. Generated
build/test locks capture each run's dependencies. Hash locking does not establish
that a package is safe.

# Snake Pit

Test a specified CPython commit against popular PyPI projects. The 28 projects,
their versions, source hashes, and test commands are configured in
[data/recipes.toml](data/recipes.toml).

The GitHub Actions pipeline accepts a CPython commit and platform list, builds
CPython once per platform, and runs a separate project test job for every pair.
The build mode defaults to `debug`; `release_assertions` and `release` are also
available. Append `-freethreading` to any mode to disable the GIL.
See [running in Actions](docs/actions.md) for inputs, artifacts, and limitations.
The project-list CLI and prepared-worker executor are also available for local tooling.

Requires Python 3.14+ for the control tools. Runtime code uses the standard library;
mypy is a locked development dependency. The control Python and the CPython
interpreter under test are separate inputs.

```sh
uv python install 3.14
uv sync --locked
uv run --locked mypy
uv run --locked python -m unittest discover -s tests -v
uv run --locked python -m snake_pit list
uv run --locked python -m snake_pit plan --commit "$CPYTHON_COMMIT" --platform "$TARGET_TRIPLE"
```

`CPYTHON_COMMIT` must be a full lowercase Git SHA, and `TARGET_TRIPLE` must be
provided explicitly. Neither has a default. The recorded Tier 1 set, checked
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

Add or edit entries in [data/recipes.toml](data/recipes.toml). Each recipe contains
an explicit `version`, `sdist_url`, and `sdist_sha256`, plus its test configuration.
When upgrading, update the version, source URL/hash, and upstream test-source
commit together. Source archives are verified before use. Actions creates jobs
only for recipe entries.

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

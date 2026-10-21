# Qualification and regression testing

The unit of evidence is `(CPython commit, build configuration, target platform,
worker image, project source revision, recipe revision, dependency locks,
native build mode, selected tests)`. A passing run belongs to that tuple. A
release's `Requires-Python`, published wheels, or upstream prerelease CI is useful
screening evidence, but does not establish compatibility with a particular commit.

## Selecting projects

The selected projects and versions live in `data/recipes.toml`, together with
source archive URLs/hashes and test configuration. Each recipe produces one job
per selected platform. Build and runtime/test dependencies are locked separately
for each run.

The project selection covers small pure Python suites, sync/async networking, compiler and
introspection behavior, NumPy's native surface, FFI, stable ABI bindings, and PyO3.
It also includes framework integration, database and filesystem behavior, other
async runtimes, parsers, native extensions, and version-sensitive internals. pip
adds packaging, import metadata, archive extraction, and subprocess behavior; its
test fixtures and installer subprocesses must use the target interpreter and
offline artifacts. A package such as Ruff is popular but its standalone
Rust executable contributes little direct CPython API exercise, so it is omitted.

Use both package-specific smoke tests and selected upstream tests. Imports alone
are insufficient. Retain the upstream suite's test count and skip/xfail reasons;
a sudden fall in executed tests is a result that needs review. Pin
the exact source revision corresponding to the recipe version.
For prerelease support available only on upstream main, create an explicitly
separate, commit-pinned development recipe.

## Qualification pipeline

The [Actions implementation](actions.md) now builds the requested interpreter and
fans out project jobs, generating per-run locks and artifacts. The steps below
describe the fuller qualification target, including baseline comparisons,
complete build-closure locking and offline isolation that are not yet automated.

1. A trusted controller accepts a full CPython SHA, one of the recorded Tier 1
   targets, a build mode, and a baseline SHA. Verify the fetched CPython commit.
   Record configure/build flags, compiler, SDK/libc, dependencies, `sys.version`,
   `sysconfig`, SOABI, debug flags, pointer width, and GIL configuration. Check
   required standard-library extensions such as `_ssl`, `_sqlite3`, and `_ctypes`.
2. Provision a native disposable VM per platform. Use a dedicated worker account,
   no credentials, no host directories or daemon sockets, bounded disk/RAM/process
   count/CPU time, and restricted artifact transfer. Linux containers can be an
   additional boundary inside a VM. Windows and macOS require native workers;
   Docker-on-Linux does not qualify them. Windows x86 must use 32-bit CPython and
   matching toolchains, even on a 64-bit worker.
3. Acquire source and dependency artifacts through an allowlisted download service,
   verifying expected hashes before use. Downloading wheels or sdists as bytes
   needs no build hooks; resolving sdist metadata can execute arbitrary hooks and
   belongs in an untrusted preparation VM. Treat archive extraction as untrusted
   work too. Do not share writable caches between projects or commits.
4. Pin source and test source separately if the sdist lacks upstream tests or
   fixtures. Record a test-source archive digest as well as its Git commit. Lock
   runtime dependencies, test runners/plugins, and each build environment using
   PEP 751 `pylock.toml` files. Include transitive dependencies in every closure.
   Projects with conflicting build requirements may need per-distribution build
   environments rather than one shared build lock.
5. Discover dynamic PEP 517 build requirements in preparation isolation, then add
   them to the build lock and repeat offline. Sync the explicit build environment
   from its lock and disable implicit build isolation/downloads during the build.
   A runtime lock alone does not pin temporary PEP 517 build environments. Do not
   disable isolation and leave build dependencies unconstrained.
6. Vendor Rust dependencies with the pinned `Cargo.lock` and checksums, and build
   with Cargo locked/offline. Pin Rust, C/C++/Fortran compilers, Meson/CMake/Cython,
   OpenSSL, libffi, BLAS, codecs, libxml2, and other native inputs in image or
   artifact manifests. `pylock.toml` does not lock Cargo or OS packages.
7. Build project and native transitive dependency wheels with the target CPython
   and headers. Save their hashes and generate target-specific installation locks
   for these exact wheels. Maintain the mapping from source lock to derived wheels
   rather than overwriting the source provenance. Rebuild rather than using a
   previous same-minor CPython wheel from a cache when testing native API changes.
8. In a fresh VM/environment with egress denied, install only locked artifacts and
   run bootstrap, build, install, import, collect, and test phases. Local socket
   servers remain usable; internet denial need not disable loopback. Record the
   exact tests excluded for internet, databases, privileges, TTYs, or platform
   support. Separate local TLS fixtures from public network tests.
9. Qualify the same recipe on the baseline and target with the same dependency
   versions and source hashes. Rebuilt wheel hashes will differ. Promote a recipe
   only after reproducible nonempty test execution and reviewed exclusions. Record
   unsupported dependencies as blockers rather than silently dropping projects.

The [pylock specification](https://packaging.python.org/en/latest/specifications/pylock-toml/)
defines reproducible installation inputs. [uv's CLI](https://docs.astral.sh/uv/reference/cli/)
can compile requirements into `pylock.toml` and sync from them. Validate the full
lock with the chosen installer, pin the installer version, and exercise both lock
generation and consumption during qualification. The worker's preliminary lock
check only recognizes the file format; it is not a dependency or schema validator.

## Native compatibility variants

Keep at least two independent lanes: **native-source**, compiling against the
exact target interpreter, and **stable-ABI**, exercising compatible ABI3 bindings.
Optionally add a **published-wheel** lane to test ordinary installation. Report
them separately: passing on an existing ABI3 wheel says nothing about whether the
project builds against newly changed native headers. Pure Python fallback variants
for PyYAML, msgpack, SQLAlchemy, and tracers must also be named explicitly.

For PyO3, inspect the version in the project's pinned Cargo dependency graph.
Prefer an upstream version that supports the interpreter. For compatible versions
on GIL-enabled CPython, `PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1` may opt into a
limited-API build; this is an attempted workaround, not guaranteed support. Scope
it to the relevant build environment and record it in the result. Projects using
non-limited APIs may still fail. Do not apply it to every Rust package: orjson's
native API choices must be assessed separately.

Newer PyO3 also has `PYO3_USE_STABLE_ABI_FORWARD_COMPATIBILITY`, and version-specific
support for free-threaded stable ABI (`abi3t`). The correct path depends on both
the CPython and PyO3 versions. Do not reuse a GIL-enabled ABI3 wheel for a
free-threaded interpreter or assume the older environment variable covers it.
See the [PyO3 build script](https://docs.rs/crate/pyo3-ffi/0.29.2/source/build.rs)
and [changelog](https://pyo3.rs/v0.30.1/changelog). Never silently bypass a version
check or replace a failed native run with a successful fallback.

## Test adapters and attribution

Recipes should use explicit argument arrays with the target interpreter, working
directory, environment, timeout, test selection, and expected report path. Common
adapters are `python -m pytest`, `python -m unittest`, Django's
`python tests/runtests.py`, and NumPy's installed-package test entry point.
Tox/nox can document upstream setup; letting them independently resolve or download
interpreters would defeat the controlled run. Translate their setup into a pinned
recipe or explicitly force the interpreter and offline dependencies.

Test the installed wheel from outside the source tree where practical. Otherwise
verify import paths and loaded extension origins so a source-tree fallback cannot
mask a bad wheel. Avoid plugin autoloading beyond the locked set, and record any
required plugin entry points. Preserve JUnit/JSON output where supported; don't
scrape human-readable output as the only evidence of test counts.

Results need independent stage and outcome fields: resolution/build/install/import/
collection/test failure, crash, timeout, resource exhaustion, unsupported platform,
infrastructure error, and pass-with-exclusions. A signal alone does not identify
the responsible component. Capture failing distribution, dependency chain, tool
versions, stderr, signal or NTSTATUS, OOM evidence from the VM, test IDs, and crash
artifacts. The current primitive captures phase, exit status, bounded logs and
provenance; richer test, OOM, and crash-dump collection is still to implement.

A baseline pass plus target failure is a regression candidate, not automatic proof
of a CPython bug. Re-run flaky failures with a recorded seed and attempt ID, keep
the original result, and minimize the failing case. Maintain separate raw and
workaround results; track a workaround's reason, upstream issue, scope, and expiry.
Interpreter build failure is reported once at the build level, not as 28 package
failures. Successful qualification on one Tier 1 target does not qualify the others.

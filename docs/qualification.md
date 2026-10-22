# Release qualification: October 21–22, 2026

Target: CPython `5a17b2ecfd5f6ad72d3c272f951c4a5b6f8e1c46`, mode `release`,
all five recorded Tier 1 platforms. These findings describe the pinned recipe
versions and selected suites, rather than every optional upstream test.

The [first iteration](https://github.com/emmatyping/snake_pit/actions/runs/39685968944)
validated all five native interpreter builds and exposed setup problems. The
[second iteration](https://github.com/emmatyping/snake_pit/actions/runs/39687839593)
validated many corrected recipes. The
[third iteration](https://github.com/emmatyping/snake_pit/actions/runs/39689854274)
contains further bootstrap and collection fixes.

Earlier runs were partially blocked by GitHub billing availability. That was
restored for [run 39700564791](https://github.com/emmatyping/snake_pit/actions/runs/39700564791):
all five CPython builds passed, and no project jobs were rejected before starting.
The [follow-up run](https://github.com/emmatyping/snake_pit/actions/runs/39702664871)
tests further Windows native dependencies, line-ending preservation, lxml helpers,
psutil plugin settings and streaming diagnostics. The results below distinguish
recorded passes from corrections awaiting hosted verification.

## Verified suites

“POSIX” below includes macOS ARM64, Linux ARM64, and Linux x86-64. Results come
from the second iteration unless stated otherwise; uploaded artifacts contain exact dependency locks,
phase logs and JUnit reports where supported.

| Projects | Passing platforms |
| --- | --- |
| packaging, Click, coverage, greenlet, Hypothesis, MessagePack | All five platforms, verified in run 39700564791 |
| Click, coverage, Django's selected suites, greenlet, MessagePack, orjson, pandas' selected array tests, Pillow | All three POSIX platforms |
| PyYAML | All three POSIX platforms, verified in run 39700564791 |
| NumPy | macOS ARM64, Linux ARM64, Linux x86-64, Windows x86, verified in run 39700564791 |
| SQLAlchemy | Linux x86-64 (third iteration), macOS ARM64 (run 39700564791); other platform results are not recorded here |

## Confirmed compatibility findings

| Component | Observed incompatibility | Evidence |
| --- | --- | --- |
| CFFI 2.1.1 | Compiles against the removed `Py_FileSystemDefaultEncoding` variable; fails on POSIX and Windows. This also blocks test/build dependencies of urllib3, HTTPX, AnyIO, cryptography, requests, Trio, uvloop and pip. On Windows it also blocks Jinja and websockets through Trio. | [Linux build](https://github.com/emmatyping/snake_pit/actions/runs/39687839593/job/119528809107), [Windows build](https://github.com/emmatyping/snake_pit/actions/runs/39687839593/job/119529620780) |
| pytest-asyncio 1.4.0 | Calls removed `asyncio.get_event_loop_policy()`. Jinja and websockets collect successfully on POSIX, then fail in this plugin's fixture. | [Jinja report](https://github.com/emmatyping/snake_pit/actions/runs/39687839593/job/119528622303), [websockets report](https://github.com/emmatyping/snake_pit/actions/runs/39687839593/job/119528809150) |
| Pydantic Core 2.50.1 | Tests fail when writing the now-read-only `uuid.UUID.int` attribute. Builds and collection succeed. | [Linux tests](https://github.com/emmatyping/snake_pit/actions/runs/39687839593/job/119528809216), [Windows x86 tests](https://github.com/emmatyping/snake_pit/actions/runs/39687839593/job/119529620631) |
| elementpath, through pytest's xmlschema tests | Emits `UnicodeWarning` for Python 3.16's Unicode 18 data. Upstream's warnings-as-errors policy initially stopped collection. The requested `-W default` recipe now allows the fallback while retaining the warning. | [pytest collection](https://github.com/emmatyping/snake_pit/actions/runs/39687839593/job/119528809167) |

Additional findings in run 39700564791:

- NumPy's Windows x64 suite fails an `exp2` floating-point exception assertion
  ([tests](https://github.com/emmatyping/snake_pit/actions/runs/39700564791/job/119570487307)).
- orjson 3.13.0 cannot link on Windows: `_PyLong_AsByteArray`,
  `_PyDict_NewPresized` and `_PyDict_SetItem_KnownHash_LockHeld` are unresolved
  ([x64 build](https://github.com/emmatyping/snake_pit/actions/runs/39700564791/job/119570488993)).
- psutil's Windows test extras require pywin32, which has neither a CPython 3.16
  wheel nor a source distribution
  ([resolver](https://github.com/emmatyping/snake_pit/actions/runs/39700564791/job/119570489070)).
- pandas 3.0.6's dateutil timezone tests started failing after python-dateutil
  3.0.0 was released. Its prior successful lock used 2.9.0.post0 with the same
  NumPy and pandas versions. The recipe now requires `python-dateutil<3` to keep
  that dependency's changed timezone internals separate from interpreter testing;
  verification of this constraint is pending.

These are incompatibilities with the tested revision. They do not establish that
the particular target commit introduced a CPython regression. No package patches,
warning suppression or exclusions hide these findings.

## Setup corrections

Interpreter artifacts now include installed development layouts on POSIX and
Windows. Project jobs retain locked build tools, seed backends before compiling
source-only build dependencies, initialize the target's MSVC environment, and
retain the end of truncated logs. Windows uses UTF-8 output and explicit `cl`
compiler selection, avoiding the hosted image's broken Strawberry Perl ccache.

Recipes correct moved test directories, import modes, working directories, and
missing test dependencies. SQLAlchemy's first macOS report had 27,228 passing
tests, 1,027 skips and 70 setup errors caused solely by missing mypy; mypy has
been added. The Windows x86 SQLAlchemy report similarly had 26,970 passing
tests and the same 70 missing-mypy setup errors. Hypothesis's missing snapshot
fixture is supplied by syrupy, verified by a passing Linux x86-64 run. NumPy's namespace test directories now collect
successfully using installed filesystem paths rather than `--pyargs`. Its next
run reached tests but could not launch the installed `meson` executable; the
harness now activates the venv's executable directory in `PATH` and records
`VIRTUAL_ENV` for all project subprocesses. This fix is verified on the four passing NumPy platforms above.

The latest corrections additionally preserve Windows application-directory
environment variables, provide Pillow's native JPEG/zlib libraries with vcpkg,
install psutil's upstream test extras and override disabled-plugin options, and run lxml's native
`test_suite()` factories with source helpers while retaining installed extensions.
These last corrections have local harness validation but still require hosted
verification. Earlier pytest collection of lxml's uninitialized base classes and
relocated doctest fixtures was a recipe failure, not a compatibility finding.

Further fixes in the October 22 follow-up provide maturin for transitive
cryptography source builds, alias vcpkg static library filenames for Pillow,
use lxml's supported Windows static dependency setup, expose both `lxml.tests`
and `lxml.html.tests` from the pinned source tree, and disable Git's automatic
line-ending conversion for upstream fixtures. Unbuffered streaming logs preserve
diagnostics even if a failed install leaves a descendant holding its output pipe.
Control CI for commit e6963cf passes on Linux, macOS and Windows.

## Local Python 3.16 follow-up

These checks use a fresh Linux x86-64 CPython build at
`630920e45e2ee9fe9bef44bf4a70c95868c58afb`, rather than the commit used in the
Actions runs above.

The local CFFI checkout (`7ac2b18fa2212052315a904ef5000f34edbd5ddf` plus the user's
filesystem-encoding replacement, with the corrected `0x030E0000` guard) builds
successfully: 1,972 tests pass, 112 skip and 4 are expected failures. Loading a
library with a non-ASCII filename also succeeds. The patch retains the new
reference returned by `PyConfig_Get()`, leaking approximately 460 KB over 10,000
load/close cycles. A temporary cleanup with error checks, `const char *` and
`Py_DECREF()` removes that measured leak on both success and argument-error paths.
The user's checkout was not modified by these checks.

At the user's request, pytest's recipe now runs with `-W default`: warnings stay
visible, and elementpath can execute its Unicode-category fallback. Collection
succeeds. The recipe also supplies upstream's `mock` dependency, uses its expected
`prepend` import mode, and lets its faulthandler tests control interpreter startup
instead of inheriting the harness's forced setting. The first run exposed an
incompatibility in mock 5.2.0, which imports the removed
`asyncio.set_event_loop_policy`. With the corrected recipe, the full suite finishes
with 4,129 passes, 6 failures, 76 skips, 16 expected failures and 4 unexpected passes.
Five failures are from mock's removed-API import; the sixth is an async unittest
integration failure (`set_event_loop()` rejects a `_UnixSelectorEventLoop` instance).
That test passes when rerun alone, so it is recorded as an order-dependent
interaction, not attributed to a CPython regression.
The earlier import-name and forced-faulthandler failures no longer occur. The
Unicode warning remains visible in the report.

# Release qualification: October 21, 2026

Target: CPython `5a17b2ecfd5f6ad72d3c272f951c4a5b6f8e1c46`, mode `release`,
all five recorded Tier 1 platforms. These findings describe the pinned recipe
versions and selected suites, rather than every optional upstream test.

The [first iteration](https://github.com/emmatyping/snake_pit/actions/runs/39685968944)
validated all five native interpreter builds and exposed setup problems. The
[second iteration](https://github.com/emmatyping/snake_pit/actions/runs/39687839593)
validated many corrected recipes. The
[third iteration](https://github.com/emmatyping/snake_pit/actions/runs/39689854274)
contains further bootstrap and collection fixes.

GitHub stopped starting many jobs with the annotation “recent account payments
have failed or your spending limit needs to be increased.” See the
[Windows job annotation](https://github.com/emmatyping/snake_pit/actions/runs/39687839593/job/119529622564).
Jobs with no steps or logs under this restriction are infrastructure failures;
they do not establish package incompatibility. Further complete qualification
requires restoring Actions billing availability and dispatching the latest main.
At the last check, the third iteration still had SQLAlchemy and pandas running
on Linux x86-64; other platform jobs had been rejected before starting. The
latest NumPy executable-path, native lxml, psutil and Windows Pillow corrections
are pushed but need another hosted run. Existing runs use their original
workflow commit, so rerunning an older attempt does not test these changes.

## Verified suites

“POSIX” below includes macOS ARM64, Linux ARM64, and Linux x86-64. Results come
from the second iteration; its uploaded artifacts contain exact dependency locks,
phase logs and JUnit reports where supported.

| Projects | Passing platforms |
| --- | --- |
| packaging | All five platforms |
| Click, coverage, Django's selected suites, greenlet, MessagePack, orjson, pandas' selected array tests, Pillow | All three POSIX platforms |
| PyYAML, Hypothesis | Linux x86-64, verified in the third iteration |

## Confirmed compatibility findings

| Component | Observed incompatibility | Evidence |
| --- | --- | --- |
| CFFI 2.1.1 | Compiles against the removed `Py_FileSystemDefaultEncoding` variable; fails on POSIX and Windows. This also blocks test/build dependencies of urllib3, HTTPX, AnyIO, cryptography, requests, Trio, uvloop and pip. On Windows it also blocks Jinja and websockets through Trio. | [Linux build](https://github.com/emmatyping/snake_pit/actions/runs/39687839593/job/119528809107), [Windows build](https://github.com/emmatyping/snake_pit/actions/runs/39687839593/job/119529620780) |
| pytest-asyncio 1.4.0 | Calls removed `asyncio.get_event_loop_policy()`. Jinja and websockets collect successfully on POSIX, then fail in this plugin's fixture. | [Jinja report](https://github.com/emmatyping/snake_pit/actions/runs/39687839593/job/119528622303), [websockets report](https://github.com/emmatyping/snake_pit/actions/runs/39687839593/job/119528809150) |
| Pydantic Core 2.50.1 | Tests fail when writing the now-read-only `uuid.UUID.int` attribute. Builds and collection succeed. | [Linux tests](https://github.com/emmatyping/snake_pit/actions/runs/39687839593/job/119528809216), [Windows x86 tests](https://github.com/emmatyping/snake_pit/actions/runs/39687839593/job/119529620631) |
| elementpath, through pytest's xmlschema tests | Emits `UnicodeWarning` for Python 3.16's Unicode 18 data. Pytest's warning policy promotes this to a collection error. | [pytest collection](https://github.com/emmatyping/snake_pit/actions/runs/39687839593/job/119528809167) |

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
`VIRTUAL_ENV` for all project subprocesses. This last fix needs hosted verification.

The latest corrections additionally preserve Windows application-directory
environment variables, provide Pillow's native JPEG/zlib libraries with vcpkg,
install psutil's upstream test extras and re-enable JUnit, and run lxml's native
`test_suite()` factories with source helpers while retaining installed extensions.
These last corrections have local harness validation but still require hosted
verification. Earlier pytest collection of lxml's uninitialized base classes and
relocated doctest fixtures was a recipe failure, not a compatibility finding.

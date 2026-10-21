# Prepared job contract

The end-to-end [Actions pipeline](actions.md) accepts a commit and platform list;
it builds CPython and prepares each project's inputs automatically. This document
covers the lower-level interface for independently prepared bundles.

The worker executor runs reviewed recipes inside an externally provisioned,
disposable VM. It does not fetch sources, build CPython, resolve dependencies,
extract archives, or enforce an OS sandbox. It has been tested locally with trusted
synthetic commands; execution on macOS and Windows has not been verified.

Run the CLI inside a worker provisioned by an external VM/container controller, with
the required filesystem, network, and resource restrictions already enforced.

A `job.json` is a JSON object with these required fields:

| Field | Meaning |
| --- | --- |
| `project` | Selected project name or explicitly named fixture |
| `project_commit` | Full Git SHA for upstream test source |
| `cpython_commit` | Full Git SHA from the trusted CPython build provenance |
| `platform` | One target triple from `data/platforms.json` |
| `mode` | `debug`, `release_assertions`, or `release`, optionally suffixed with `-freethreading` |
| `worker_image` | Immutable image/snapshot identifier, supplied by controller |
| `python` | Absolute path to built target interpreter |
| `python_sha256` | Hash supplied by trusted CPython builder, checked before commands |
| `artifacts` | Map of bundle-relative file paths to SHA256 values |
| `locks` | Object with `build`, `runtime`, `test` paths included in `artifacts` |
| `phases` | Ordered phase objects: `bootstrap`, `build`, `install`, `import`, `collect`, `test` |
| `env` | Optional explicit environment variables for the reviewed recipe |

Each phase has a `name`, nonempty `argv` string array, positive `timeout_seconds`
(at most one day), optional bundle-relative `cwd`, and optional `framework` set
to `pytest` for pytest-specific exit descriptions. Every phase is mandatory so a
job cannot accidentally omit imports, collection, or tests. A prebuilt/irrelevant
phase can use a reviewed provenance-check command. A zero exit status is the
primitive's success criterion; qualification additionally requires a parsed,
nonempty test report and reviewed skips.

Use absolute target interpreter/tool paths in executable recipes. A pytest test
phase might have `argv` equal to `['/opt/cpython/bin/python3', '-m', 'pytest',
'--junitxml=/results/tests.xml', '/bundle/source/tests']`. Actual paths, runner
plugins, and test selections are project-specific. Do not assume this example is
a valid upstream recipe.

Artifacts should include source archives, test-source archives, all locks, wheel
inputs, Cargo manifests/vendor manifests where applicable, build provenance and
patches. The executor checks all declared file hashes, but it does not prove the
artifact map is complete or that a command uses a lock. The controller must review
recipes, validate locks with the installer, enforce offline inputs, and bind image
provenance to the requested commit/platform/mode. A self-declared SHA or image name
is not independent evidence that those inputs were actually built or used.

Commands run without a shell and without inherited credential or Python-path
variables. Explicit `env` values can configure ABI variants and required plugins.
Installer offline settings are not security boundaries; malicious code can ignore
them. The VM must deny egress and have no secrets regardless of recipe behavior.
Protect the result directory's capacity with an external disk quota. Logs retain
at most 4 MiB per phase while draining further output to avoid pipe deadlocks.

`--output` must name a new directory. `result.json` is updated after each phase;
failure leaves later phases skipped. It includes the full job, job hash, input
digests, durations, logs, and exit codes. POSIX signals and Windows exception exit
codes are reported as crashes. A positive exit code such as 137 is left as a
failure rather than guessed to be an OOM. Test frameworks can absorb child crashes,
so their reports are still required for detailed diagnosis.

On POSIX, each phase gets a process group which is killed on completion or timeout.
Windows uses `taskkill /T /F` on timeout; hardened Windows deployments should also
use a Job Object with kill-on-close outside this primitive. All platforms require
external wall-time/resource enforcement and VM teardown, including when the
controller dies, processes escape the group, or a job tampers with worker-local
logs. Treat exported results as untrusted data; host artifact ingestion should
validate size, paths, types, and encoding. No host-mounted result directory should
be exposed directly to potentially malicious workloads.

"""Static contracts for projects, planner, and prepared-worker documents.

JSON/TOML casts at read boundaries describe expected shapes; worker validation still
checks executable inputs before running commands.
"""
from typing import NotRequired, TypedDict


class Sdist(TypedDict):
    filename: str
    url: str
    sha256: str


class Project(TypedDict):
    name: str
    release: str
    sdist: Sdist


class Recipe(TypedDict):
    version: str
    sdist_url: str
    sdist_sha256: str
    repository: str
    tag: str
    module: str
    tests: str
    dependencies: list[str]
    build_dependencies: NotRequired[list[str]]
    import_mode: NotRequired[str]
    pytest_args: NotRequired[list[str]]
    test_env: NotRequired[dict[str, str]]
    installed: NotRequired[bool]
    adapter: NotRequired[str]
    selection: NotRequired[list[str]]
    linux: NotRequired[list[str]]
    macos: NotRequired[list[str]]
    windows: NotRequired[list[str]]
    source: NotRequired[str]
    commit: NotRequired[str]


class PlatformInfo(TypedDict):
    os: str
    compiler: str
    libc: NotRequired[str]


class PlatformPolicy(TypedDict):
    source: str
    checked_on: str
    platforms: dict[str, PlatformInfo]


class PlanJob(TypedDict):
    project: str
    release: str
    status: str


class Plan(TypedDict):
    cpython_commit: str
    platform: str
    mode: str
    platform_policy: PlatformPolicy
    recipes_sha256: str
    status: str
    jobs: list[PlanJob]


class Phase(TypedDict):
    name: str
    argv: list[str]
    timeout_seconds: float
    cwd: NotRequired[str]
    framework: NotRequired[str]


class Locks(TypedDict):
    build: str
    runtime: str
    test: str


class Job(TypedDict):
    project: str
    project_commit: str
    cpython_commit: str
    platform: str
    mode: str
    worker_image: str
    python: str
    python_sha256: str
    artifacts: dict[str, str]
    locks: Locks
    phases: list[Phase]
    env: NotRequired[dict[str, str]]


class PhaseResult(TypedDict):
    name: str
    argv: list[str]
    status: str
    duration_seconds: NotRequired[float]
    error: NotRequired[str]
    detail: NotRequired[str]
    returncode: NotRequired[int]
    log: NotRequired[str]
    log_bytes: NotRequired[int]
    log_truncated: NotRequired[bool]


class RunResult(TypedDict):
    schema_version: int
    project: str
    cpython_commit: str
    platform: str
    mode: str
    status: str
    started_at: str
    job_sha256: str
    provenance: Job
    phases: list[PhaseResult]
    failed_phase: NotRequired[str]
    skipped_phases: NotRequired[list[str]]

"""Execution primitive for a disposable VM. This module does not create a sandbox."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import tomllib
from typing import cast

from .models import Job, Phase, PhaseResult, RunResult

LOG_LIMIT = 4 * 1024 * 1024
PHASES = ('bootstrap', 'build', 'install', 'import', 'collect', 'test')


def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inside(root: Path, name: str) -> Path:
    root = root.resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f'path outside job bundle: {name}')
    return path


def validate(job: Job, root: Path) -> None:
    from .__main__ import plan
    plan(job['cpython_commit'], job['platform'], job['mode'])
    if not isinstance(job['worker_image'], str) or not job['worker_image']:
        raise ValueError('record an immutable worker image identifier')
    if not re.fullmatch(r'[0-9a-f]{40}', job['project_commit']):
        raise ValueError('record the full upstream test-source commit')
    artifacts = job['artifacts']
    if not artifacts:
        raise ValueError('a prepared job must record hashed artifacts')
    for name, expected in artifacts.items():
        if not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ValueError(f'invalid SHA256: {name}')
        if sha256(inside(root, name)) != expected:
            raise ValueError(f'artifact digest mismatch: {name}')
    for role in ('build', 'runtime', 'test'):
        name = job['locks'][role]
        if name not in artifacts or not re.fullmatch(r'pylock(?:\.[^.]+)?\.toml', Path(name).name):
            raise ValueError(f'{role} must refer to a hashed pylock file')
        lock = tomllib.loads(inside(root, name).read_text())
        if lock.get('lock-version') != '1.0' or not isinstance(lock.get('packages'), list):
            raise ValueError(f'{role} is not a supported pylock file')
    python = Path(job['python'])
    if not python.is_absolute() or sha256(python) != job['python_sha256']:
        raise ValueError('target Python must be absolute and match its build provenance hash')
    if [p['name'] for p in job['phases']] != list(PHASES):
        raise ValueError(f'phases must be ordered as {PHASES}')
    for phase in job['phases']:
        argv = phase['argv']
        if not isinstance(argv, list) or not argv or any(not isinstance(a, str) for a in argv):
            raise ValueError('phase argv must be a nonempty string array')
        if not 0 < phase['timeout_seconds'] <= 86400:
            raise ValueError('phase timeout must be in (0, 86400]')
        if not inside(root, phase.get('cwd', '.')).is_dir():
            raise ValueError('phase cwd must exist inside the bundle')


def stop_tree(process: subprocess.Popen[bytes]) -> None:
    if sys.platform != 'win32':
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        # Disposable Windows VM teardown is still required for escaped descendants.
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       capture_output=True, timeout=20, check=False)


def run_phase(phase: Phase, root: Path, output: Path, env: dict[str, str]) -> PhaseResult:
    start = time.monotonic()
    result: PhaseResult = dict(name=phase['name'], argv=phase['argv'], status='running')
    log = output / f"{phase['name']}.log"
    try:
        process = subprocess.Popen(phase['argv'], cwd=inside(root, phase.get('cwd', '.')),
                                   env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=os.name == 'posix')
    except OSError as exc:
        result.update({'status': 'infrastructure-error', 'error': str(exc),
                       'duration_seconds': time.monotonic() - start})
        return result
    counts = {'log_bytes': 0}

    pipe = process.stdout
    assert pipe is not None  # Popen was configured with stdout=PIPE.

    def drain() -> None:
        tail = bytearray()
        with pipe, log.open('wb') as stream:
            while chunk := pipe.read(65536):
                before = counts['log_bytes']
                counts['log_bytes'] += len(chunk)
                stream.write(chunk[:max(0, LOG_LIMIT - before)])
                tail.extend(chunk)
                del tail[:-65536]
        # Retain final diagnostics even when verbose collection/build output fills
        # the primary log before the error is emitted.
        if counts['log_bytes'] > LOG_LIMIT:
            log.with_suffix('.tail.log').write_bytes(tail)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        process.wait(timeout=phase['timeout_seconds'])
        result['status'] = 'passed' if process.returncode == 0 else 'failed'
        if process.returncode < 0 or (os.name == 'nt' and process.returncode & 0xC0000000 == 0xC0000000):
            result['status'] = 'crashed'
        if phase.get('framework') == 'pytest' and process.returncode in (2, 3, 4, 5):
            result['detail'] = {2: 'interrupted-or-collection-error', 3: 'pytest-internal-error',
                                4: 'pytest-usage-error', 5: 'no-tests-collected'}[process.returncode]
    except subprocess.TimeoutExpired:
        result['status'] = 'timeout'
    finally:
        stop_tree(process)
        process.wait(timeout=20)
        reader.join(timeout=5)
    if reader.is_alive():
        result['status'] = 'infrastructure-error'
        result['detail'] = 'descendant kept output pipe open; destroy worker'
    result.update({'returncode': process.returncode, 'duration_seconds': time.monotonic() - start,
                   'log': log.name, 'log_bytes': counts['log_bytes'],
                   'log_truncated': counts['log_bytes'] > LOG_LIMIT})
    return result


def run_job(job_path: Path, output: Path) -> RunResult:
    job_path = job_path.resolve()
    root = job_path.parent
    job = cast(Job, json.loads(job_path.read_text()))
    validate(job, root)  # All inputs are checked before any candidate command executes.
    output.mkdir(parents=True, exist_ok=False)
    # Do not pass host credentials, PYTHONPATH, or installer settings to package code.
    env = {k: os.environ[k] for k in ('PATH', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR')
           if k in os.environ}
    env.update(HOME=str(output.resolve()), USERPROFILE=str(output.resolve()),
               PYTHONFAULTHANDLER='1', PYTHONUNBUFFERED='1',
               UV_OFFLINE='1', PIP_NO_INDEX='1', CARGO_NET_OFFLINE='true')
    env.update(job.get('env', {}))
    result: RunResult = dict(schema_version=1, project=job['project'], cpython_commit=job['cpython_commit'],
                  platform=job['platform'], mode=job['mode'], status='running',
                  started_at=datetime.now(timezone.utc).isoformat(),
                  job_sha256=sha256(job_path), provenance=job, phases=[])

    def save() -> None:
        temp = output / 'result.tmp'
        temp.write_text(json.dumps(result, indent=2) + '\n')
        temp.replace(output / 'result.json')

    save()
    for phase in job['phases']:
        current = run_phase(phase, root, output, env)
        result['phases'].append(current)
        if current['status'] != 'passed':
            result['status'] = current['status']
            result['failed_phase'] = phase['name']
            result['skipped_phases'] = list(PHASES[len(result['phases']):])
            save()
            return result
        save()
    result['status'] = 'passed'
    save()
    return result

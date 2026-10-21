"""Shared CPython build modes for CLI arguments, manifests and workflows."""
from dataclasses import dataclass

DEFAULT_MODE = 'debug'
MODES = (
    'debug', 'release_assertions', 'release',
    'debug-freethreading', 'release_assertions-freethreading', 'release-freethreading',
)


@dataclass(frozen=True)
class BuildMode:
    debug: bool
    assertions: bool
    freethreading: bool


def parse_mode(mode: str) -> BuildMode:
    if mode not in MODES:
        raise ValueError(f'unknown CPython mode: {mode}; choose from {", ".join(MODES)}')
    base = mode.removesuffix('-freethreading')
    return BuildMode(debug=base == 'debug', assertions=base != 'release',
                     freethreading=mode.endswith('-freethreading'))

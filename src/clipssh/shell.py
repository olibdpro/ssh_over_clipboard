"""Shell process management for the clipboard server."""

from sshcore.shell import (
    MARKER_PREFIX,
    ShellExecutionError,
    ShellSession,
    ShellUnavailableError,
    resolve_shell,
)

__all__ = [
    "MARKER_PREFIX",
    "ShellExecutionError",
    "ShellSession",
    "ShellUnavailableError",
    "resolve_shell",
]

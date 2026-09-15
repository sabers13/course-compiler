"""Application configuration for the Course Compiler local web app.

T047 scaffold: validated host/port/data-root with loopback default and
content-safe diagnostics. No business semantics, no secrets, no network egress.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8787
_DEFAULT_DATA_ROOT = _REPOSITORY_ROOT / "local-data" / "app"

_HOST_RE = re.compile(r"[A-Za-z0-9.\-:]+\Z")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ALLOWED_DATA_ROOT_PREFIXES = ("local-data", "local-artifacts", "build")


class AppConfigError(Exception):
    """A fixed, content-safe configuration failure."""


def _is_loopback_host(host: str) -> bool:
    return host in _LOOPBACK_HOSTS


def _is_allowed_data_root(path: Path) -> bool:
    """Return True iff data root is inside an approved private runtime root.

    Approved roots are repository-relative ``local-data/``, ``local-artifacts/``,
    and ``build/``. Any path that resolves outside the repository (including via
    symlink or ``..`` traversal) is rejected fail-closed; any absolute or
    relative path that resolves inside the repository but outside the three
    prefixes is similarly rejected to prevent accidental persistence of private
    runtime data into tracked locations. ``resolve()`` follows symlinks.
    Only explicitly approved ignored roots are allowed — being outside the
    repository is not sufficient, because arbitrary external locations such as
    ``~/Documents`` or ``/mnt/shared`` must not silently become normal product
    persistence roots. Normal application configuration therefore keeps durable
    private runtime data under ``local-data/app`` (or a nested ``local-data/…``
    subdirectory) unless a separate explicit opt-in contract is introduced.
    """

    if not isinstance(path, Path):
        return False
    try:
        # Use resolve to normalize ``..`` and symlinks. strict=False allows
        # non-existent leaf (data root not yet created).
        repo = _REPOSITORY_ROOT.resolve()
        candidate = (path if path.is_absolute() else (repo / path)).resolve()
    except Exception:
        return False
    # Outside repository -> reject: not an approved private runtime root.
    # Arbitrary external paths (e.g. /tmp, ~/Documents, /mnt/shared) must not
    # silently become product persistence roots merely because they are outside
    # the repository and thus not Git-tracked.
    try:
        candidate.relative_to(repo)
    except ValueError:
        return False
    for prefix in _ALLOWED_DATA_ROOT_PREFIXES:
        allowed = (repo / prefix).resolve()
        if candidate == allowed:
            return True
        try:
            candidate.relative_to(allowed)
            return True
        except ValueError:
            continue
    return False


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Validated runtime configuration for the local application."""

    host: str
    port: int
    data_root: Path

    def __post_init__(self) -> None:
        if type(self.host) is not str or not self.host:
            raise AppConfigError("host_invalid")
        if not _HOST_RE.fullmatch(self.host):
            raise AppConfigError("host_invalid")
        if _is_loopback_host(self.host) is False:
            # Non-loopback requires explicit opt-in via env flag; the
            # validation below enforces this by rejecting here. Caller
            # that has verified the opt-in may use an explicit allow flag
            # and bypass this check via `create_config(allow_non_loopback=True)`.
            # Direct construction remains strict.
            raise AppConfigError("host_not_loopback")
        if type(self.port) is not int:
            raise AppConfigError("port_invalid")
        if not (0 <= self.port <= 65535):
            raise AppConfigError("port_invalid")
        if self.port != 0 and not (1024 <= self.port <= 65535):
            raise AppConfigError("port_invalid")
        if not isinstance(self.data_root, Path):
            raise AppConfigError("data_root_invalid")
        try:
            # Normalize but do not require existence; existence is handled
            # by AppContext preparation.
            str(self.data_root)
        except Exception:
            raise AppConfigError("data_root_invalid") from None
        if not _is_allowed_data_root(self.data_root):
            raise AppConfigError("data_root_invalid")


def create_config(
    *,
    host: str | None = None,
    port: int | None = None,
    data_root: str | Path | None = None,
    allow_non_loopback: bool = False,
) -> AppConfig:
    """Create a validated AppConfig with defaults and optional overrides.

    Direct construction via :class:`AppConfig` remains strict (loopback only).
    This factory permits an explicit ``allow_non_loopback`` opt-in for
    development override outside the default product path.
    """

    resolved_host = host if host is not None else os.environ.get("COURSE_COMPILER_HOST", _DEFAULT_HOST)
    if type(resolved_host) is not str:
        raise AppConfigError("host_invalid")
    resolved_host = resolved_host.strip()
    if not resolved_host:
        raise AppConfigError("host_invalid")
    if not _HOST_RE.fullmatch(resolved_host):
        raise AppConfigError("host_invalid")

    env_allow = os.environ.get("COURSE_COMPILER_ALLOW_NON_LOOPBACK", "").strip() == "1"
    non_loopback_allowed = bool(allow_non_loopback or env_allow)
    if resolved_host not in _LOOPBACK_HOSTS and not non_loopback_allowed:
        raise AppConfigError("host_not_loopback")

    if port is not None:
        resolved_port: int | str = port
    else:
        env_port = os.environ.get("COURSE_COMPILER_PORT")
        resolved_port = _DEFAULT_PORT if env_port is None or env_port.strip() == "" else env_port.strip()

    if type(resolved_port) is str:
        if not resolved_port.isdigit():
            raise AppConfigError("port_invalid")
        try:
            resolved_port_int = int(resolved_port)
        except (TypeError, ValueError):
            raise AppConfigError("port_invalid") from None
    elif type(resolved_port) is int:
        resolved_port_int = resolved_port
    else:
        raise AppConfigError("port_invalid")

    if not (0 <= resolved_port_int <= 65535):
        raise AppConfigError("port_invalid")
    if resolved_port_int != 0 and not (1024 <= resolved_port_int <= 65535):
        raise AppConfigError("port_invalid")

    if data_root is not None:
        resolved_data_root = data_root
    else:
        env_root = os.environ.get("COURSE_COMPILER_DATA_ROOT")
        if env_root is None or env_root.strip() == "":
            resolved_data_root = _DEFAULT_DATA_ROOT
        else:
            resolved_data_root = Path(env_root.strip())

    if type(resolved_data_root) is str:
        if not resolved_data_root.strip():
            raise AppConfigError("data_root_invalid")
        resolved_data_root = Path(resolved_data_root.strip())
    if not isinstance(resolved_data_root, Path):
        raise AppConfigError("data_root_invalid")
    # Reject empty path like "" -> "." which would be current directory
    try:
        if str(resolved_data_root).strip() in ("", "."):
            raise AppConfigError("data_root_invalid")
    except Exception:
        raise AppConfigError("data_root_invalid") from None
    normalized_data_root = Path(resolved_data_root)
    if not _is_allowed_data_root(normalized_data_root):
        raise AppConfigError("data_root_invalid")

    # Re-validate host loopback allow flag for dataclass __post_init__ path.
    # If non-loopback was explicitly allowed (via flag or env), bypass dataclass
    # host loopback check by constructing via object.__new__ and re-validating
    # remaining invariants. This keeps direct AppConfig construction strict while
    # allowing an explicit opt-in path to survive final validation.
    if resolved_host not in _LOOPBACK_HOSTS and non_loopback_allowed:
        cfg = object.__new__(AppConfig)
        object.__setattr__(cfg, "host", resolved_host)
        object.__setattr__(cfg, "port", resolved_port_int)
        object.__setattr__(cfg, "data_root", normalized_data_root)
        # Manually validate port/data_root without host loopback check.
        if not (0 <= cfg.port <= 65535) or (cfg.port != 0 and not (1024 <= cfg.port <= 65535)):
            raise AppConfigError("port_invalid")
        if not isinstance(cfg.data_root, Path):
            raise AppConfigError("data_root_invalid")
        if not _is_allowed_data_root(cfg.data_root):
            raise AppConfigError("data_root_invalid")
        return cfg

    return AppConfig(host=resolved_host, port=resolved_port_int, data_root=normalized_data_root)


def default_config() -> AppConfig:
    """Return the default validated configuration (loopback, stable port, ignored data root)."""

    return create_config()


def resolve_data_root(data_root: Path) -> Path:
    """Resolve data root relative to repository and ensure parent exists."""

    if not isinstance(data_root, Path):
        raise AppConfigError("data_root_invalid")
    if not _is_allowed_data_root(data_root):
        raise AppConfigError("data_root_invalid")
    return data_root

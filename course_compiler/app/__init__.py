"""Course Compiler application shell package (T047).

Local web application owning HTTP server, routing, SQLite opener seam,
and browser shell. No workflow semantics; T048 owns Course/Job durability.
"""

from .config import AppConfig, AppConfigError, create_config, default_config
from .context import AppContext, AppContextError, create_app_context
from .server import CourseCompilerServer, create_server, run_server

__all__ = [
    "AppConfig",
    "AppConfigError",
    "AppContext",
    "AppContextError",
    "CourseCompilerServer",
    "create_app_context",
    "create_config",
    "create_server",
    "default_config",
    "run_server",
]

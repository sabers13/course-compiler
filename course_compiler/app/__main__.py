"""Entrypoint for `python -m course_compiler.app`."""

from __future__ import annotations

import argparse
from pathlib import Path

from .config import create_config
from .server import run_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Course Compiler local application shell.")
    parser.add_argument("--host", type=str, default=None, help="Loopback host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Port (default 8787, 0 for ephemeral)")
    parser.add_argument("--data-root", type=Path, default=None, help="Runtime data root (default local-data/app)")
    parser.add_argument("--allow-non-loopback", action="store_true", help="Allow non-loopback host (development override)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = create_config(
            host=args.host,
            port=args.port,
            data_root=args.data_root,
            allow_non_loopback=bool(args.allow_non_loopback),
        )
    except Exception as error:
        # Content-safe error output
        print(f"configuration_failed: {error}", flush=True)
        return 2
    run_server(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse

import uvicorn

from backend import config
from backend import logging_setup
from backend.app import app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BeeBox backend server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    args = parser.parse_args()
    logging_setup.configure_logging("server")
    try:
        config.validate_bind_security(args.host)
    except RuntimeError as exc:
        raise SystemExit(f"error: {exc}") from exc
    uvicorn.run("backend.main:app", host=args.host, port=args.port, reload=False, log_config=None)


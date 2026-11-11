from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import signal
import sys
import threading
import time
from pathlib import Path

import uvicorn

from backend import __version__
from backend import config
from backend import logging_setup


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is not a liveness probe on Windows (WinError 87);
        # query the process handle instead.
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED means it exists
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _start_parent_watchdog(parent_pid: int | None) -> None:
    if not parent_pid:
        return

    def watch() -> None:
        while True:
            if not _pid_alive(parent_pid):
                os.kill(os.getpid(), signal.SIGTERM)
                return
            time.sleep(2)

    threading.Thread(target=watch, daemon=True).start()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the BeeBox backend server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--parent-pid", type=int, default=None)
    parser.add_argument("--local-worker-request", default=None)
    parser.add_argument("--local-worker-result", default=None)
    parser.add_argument("--runtime-check", choices=["mlx", "all"], default=None)
    parser.add_argument("--version", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    multiprocessing.freeze_support()
    args = parse_args(argv)
    if args.version:
        print(__version__)
        return
    if args.runtime_check:
        os.environ["BEEBOX_RUNTIME_PROBE_CHILD"] = "1"
        from backend.services.platform import detect_runtime_in_process, mlx_runtime_import_error

        if args.runtime_check == "mlx":
            error = mlx_runtime_import_error()
            if error:
                print(error, file=sys.stderr)
                raise SystemExit(1)
            print("MLX runtime available")
            return
        print(json.dumps(detect_runtime_in_process(), ensure_ascii=False))
        return
    if args.data_dir:
        os.environ["BEEBOX_DATA_DIR"] = str(Path(args.data_dir).expanduser().resolve())
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    logging_setup.configure_logging("worker" if args.local_worker_request else "server")
    _start_parent_watchdog(args.parent_pid)
    if args.local_worker_request or args.local_worker_result:
        if not args.local_worker_request or not args.local_worker_result:
            parser_error = "--local-worker-request and --local-worker-result must be used together"
            raise SystemExit(parser_error)
        from backend.services.local_task_worker import run_local_task_worker

        raise SystemExit(run_local_task_worker(args.local_worker_request, args.local_worker_result))
    # A LAN/Tailscale bind without a Bearer token is refused before uvicorn
    # starts (ADR-0011); the Windows deployment passes --host 0.0.0.0.
    try:
        config.validate_bind_security(args.host)
    except RuntimeError as exc:
        raise SystemExit(f"error: {exc}") from exc
    # log_config=None keeps uvicorn's records on the root logger configured
    # above instead of installing its own non-UTF-8 console handler.
    uvicorn.run("backend.app:app", host=args.host, port=args.port, log_level="info", log_config=None)


if __name__ == "__main__":
    main(sys.argv[1:])

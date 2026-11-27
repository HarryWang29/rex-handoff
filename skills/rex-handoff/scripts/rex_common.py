#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path


REX_ERRORS = (OSError, subprocess.SubprocessError, ValueError)
APP_REX_PATH = Path("Contents/Helpers/Rex Server.rex/Contents/MacOS/rex")


def is_macos() -> bool:
    return sys.platform == "darwin"


def executable_path(path: Path) -> str | None:
    if path.is_file() and os.access(path, os.X_OK):
        return str(path.absolute())
    return None


def find_rex_bin(env: Mapping[str, str] | None = None) -> str | None:
    if not is_macos():
        return None
    if env is None:
        env = os.environ

    bin_dir = env.get("REX_BIN_DIR")
    if bin_dir:
        found = executable_path(Path(bin_dir).expanduser() / "rex")
        if found:
            return found

    found = shutil.which("rex", path=env.get("PATH"))
    if found:
        return str(Path(found).absolute())

    home = Path.home()
    current_server = home / (
        "Library/Application Support/com.superlogical.rex/"
        "RexServer/Versions/Current/Rex Server.app/Contents/MacOS/rex"
    )
    found = executable_path(current_server)
    if found:
        return found

    for app_name in ("Rex.app", "Rex Beta.app"):
        for applications_dir in (Path("/Applications"), home / "Applications"):
            found = executable_path(applications_dir / app_name / APP_REX_PATH)
            if found:
                return found

    try:
        result = subprocess.run(
            ["mdfind", "kMDItemCFBundleIdentifier == 'com.superlogical.rex'"],
            check=True,
            capture_output=True,
            text=True,
            timeout=1.5,
        )
    except REX_ERRORS:
        return None
    for line in result.stdout.splitlines():
        app = Path(line.strip())
        if app.suffix == ".app":
            found = executable_path(app / APP_REX_PATH)
            if found:
                return found
    return None


def run_rex_text(rex_bin: str, args: list[str], timeout: float = 5.0) -> str:
    return subprocess.run(
        [rex_bin, *args],
        check=True,
        capture_output=True,
        encoding="utf-8",
        timeout=timeout,
    ).stdout


def run_rex_json(
    rex_bin: str,
    args: list[str],
    append_json_flag: bool = True,
    timeout: float = 5.0,
) -> dict:
    command = [*args, "--json"] if append_json_flag else args
    data = json.loads(run_rex_text(rex_bin, command, timeout))
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object from Rex")
    return data


def run_rex(rex_bin: str, args: list[str], timeout: float = 5.0) -> None:
    run_rex_text(rex_bin, args, timeout)


def get_block_process(
    rex_bin: str, session_id: str, block_id: str, timeout: float = 5.0
) -> dict:
    return run_rex_json(
        rex_bin,
        ["block", "call", "-s", session_id, "-b", block_id, "process"],
        append_json_flag=False,
        timeout=timeout,
    )


def get_block_size(rex_bin: str, session_id: str, block_id: str) -> dict:
    return run_rex_json(
        rex_bin,
        ["block", "call", "-s", session_id, "-b", block_id, "size"],
        append_json_flag=False,
    )


def iter_window_blocks(window: dict, *, tiled_only: bool = False) -> Iterator[dict]:
    for layer in window.get("layers") or ():
        if not tiled_only or layer.get("kind") == "tiled":
            yield from layer.get("blocks") or ()


def locate_window_for_block(session_info: dict, block_id: str) -> tuple[dict | None, int]:
    for window in session_info.get("windows") or ():
        if any(block.get("block_id") == block_id for block in iter_window_blocks(window)):
            return window, sum(1 for _ in iter_window_blocks(window, tiled_only=True))
    return None, 0


def inspect_session_for_block(rex_bin: str, block_id: str) -> dict | None:
    def inspect(session_id: str | None = None) -> dict | None:
        command = ["session", "inspect"]
        if session_id:
            command.append(session_id)
        try:
            info = run_rex_json(rex_bin, command)
        except REX_ERRORS:
            return None
        window, _ = locate_window_for_block(info, block_id)
        return info if window is not None else None

    info = inspect()
    if info is not None:
        return info

    env_session_id = os.environ.get("REX_SESSION")
    if env_session_id:
        info = inspect(env_session_id)
        if info is not None:
            return info

    try:
        identity = run_rex_json(rex_bin, ["--autostart=false", "whoami"], timeout=2.0)
    except REX_ERRORS:
        return None
    identity_session_id = identity.get("session_id")
    if not identity_session_id or identity_session_id == env_session_id:
        return None
    return inspect(identity_session_id)

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rex_common import (
    REX_ERRORS,
    find_rex_bin,
    inspect_session_for_block,
    is_macos,
    locate_window_for_block,
    run_rex,
    run_rex_text,
)


TITLE_DELAYS = (0.0, 1.0, 1.5, 2.0, 2.5, 3.0)
TEMP_DIRS = tuple(map(Path, ("/tmp", "/private/tmp", "/var", "/private/var")))
DEFAULT_SESSION_LABEL = re.compile(r"[a-z]+-[a-z]+")
DEFAULT_WINDOW_LABEL = re.compile(r"Window\s+[0-9a-fA-F]{6,}")
PBTXT_TITLE = re.compile(r'^[ \t]*title[ \t]*:[ \t]*"((?:\\.|[^"\\\r\n])*)"', re.MULTILINE)
PBTXT_ESCAPES = {
    b"a": b"\a",
    b"b": b"\b",
    b"f": b"\f",
    b"n": b"\n",
    b"r": b"\r",
    b"t": b"\t",
    b"v": b"\v",
    b"\\": b"\\",
    b"'": b"'",
    b'"': b'"',
    b"?": b"?",
}


def auto_title_enabled() -> bool:
    return os.environ.get("REX_HANDOFF_AUTO_TITLE", "1").strip().lower() not in {
        "0", "false", "off",
    }


def resolve_dir_labels(cwd: str, home: str | None = None) -> tuple[str | None, str]:
    directory = Path(os.path.abspath(Path(cwd).expanduser()))
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            encoding="utf-8",
            timeout=1.5,
        )
        root = result.stdout.rstrip("\r\n")
        if root:
            label = Path(root).name or "/"
            return label, label
    except REX_ERRORS:
        pass

    home_dir = Path(os.path.abspath(Path(home).expanduser())) if home else Path.home()
    if directory == home_dir:
        return None, "~"

    basename = directory.name or "/"
    if directory == Path("/") or any(
        directory == temporary or temporary in directory.parents for temporary in TEMP_DIRS
    ):
        return None, basename
    return basename, basename


def state_directory() -> Path:
    return Path(f"/tmp/rex-handoff-title-state-{os.getuid()}")


def state_path(prefix: str, identity: str) -> Path:
    directory = state_directory()
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError(f"Rex Handoff needs a private state directory at {directory}")

    safe_key = identity
    if not re.fullmatch(r"[A-Za-z0-9_-]+", safe_key):
        safe_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return directory / f"{prefix}-{safe_key}.txt"


def read_state(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def write_state(path: Path, value: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as file:
            temporary = Path(file.name)
            file.write(value)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def maybe_sync_initial_session_label(
    rex_bin: str, session_info: dict, block_count: int, cwd: str
) -> str | None:
    if len(session_info.get("windows") or ()) != 1 or block_count > 1:
        return None
    session_id = session_info.get("session_id")
    current = session_info.get("label")
    if not session_id or not isinstance(current, str):
        return None

    cache = state_path("session", session_id)
    previous = read_state(cache)
    if previous and current != previous:
        return None

    label, tab_label = resolve_dir_labels(cwd)
    if label:
        label = label.strip()
        if label and label != current:
            if not previous and not DEFAULT_SESSION_LABEL.fullmatch(current):
                return tab_label
            run_rex(rex_bin, ["session", "rename", session_id, "--", label])
            write_state(cache, label)
    return tab_label


def decode_pbtxt_string(raw: str) -> str:
    def decode_escape(match: re.Match[bytes]) -> bytes:
        escape = match[1]
        if escape[0] in b"01234567":
            return bytes((int(escape, 8),))
        if escape.startswith(b"x") and len(escape) > 1:
            return bytes((int(escape[1:], 16),))
        return PBTXT_ESCAPES.get(escape, match[0])

    decoded = re.sub(rb"\\([0-7]{1,3}|x[0-9a-fA-F]{1,2}|.)", decode_escape, raw.encode("utf-8"))
    return decoded.decode("utf-8")


def read_agy_title(conversation_id: str, agy_dir: Path | None = None) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", conversation_id):
        return ""
    if agy_dir is None:
        agy_dir = Path.home() / ".gemini/antigravity-cli"

    try:
        annotation = (agy_dir / "annotations" / f"{conversation_id}.pbtxt").read_text(
            encoding="utf-8"
        )
        match = PBTXT_TITLE.search(annotation)
        if match:
            title = decode_pbtxt_string(match[1]).strip()
            if title:
                return title
    except (OSError, ValueError):
        pass

    try:
        metadata = json.loads(
            (agy_dir / "cache/conversation_metadata.json").read_text(encoding="utf-8")
        )
        conversations = metadata.get("conversations", metadata)
        title = conversations[conversation_id]["summary"]["Title"]
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        return ""
    return title.strip() if isinstance(title, str) else ""


def sync_agy_title_bg(conversation_id: str, cwd: str | None = None, retries: int = 0) -> None:
    expected_session_id = os.environ.get("REX_SESSION") or None
    block_id = os.environ.get("REX_BLOCK")
    if not is_macos() or not auto_title_enabled() or not block_id:
        return
    rex_bin = find_rex_bin()
    if not rex_bin:
        return

    if cwd:
        info = inspect_session_for_block(rex_bin, block_id)
        session_id = info.get("session_id") if info else None
        if info and session_id and (not expected_session_id or session_id == expected_session_id):
            _, tiled_count = locate_window_for_block(info, block_id)
            maybe_sync_initial_session_label(rex_bin, info, tiled_count, cwd)

    if not conversation_id:
        return
    cache = state_path("block-agy", block_id)
    previous = read_state(cache)
    for delay in TITLE_DELAYS[: retries + 1]:
        if delay:
            time.sleep(delay)
        title = read_agy_title(conversation_id)
        if not title or title == previous:
            continue

        info = inspect_session_for_block(rex_bin, block_id)
        session_id = info.get("session_id") if info else None
        if not info or not session_id or (expected_session_id and session_id != expected_session_id):
            return
        window, _ = locate_window_for_block(info, block_id)
        if not window or not window.get("window_id"):
            return
        window_id = window["window_id"]
        if window.get("label") != title:
            run_rex(rex_bin, ["window", "rename", "-s", session_id, window_id, "--", title])
        write_state(state_path("win-agent", window_id), block_id)
        write_state(cache, title)
        return


def sync_shell_cwd(cwd: str) -> None:
    expected_session_id = os.environ.get("REX_SESSION") or None
    block_id = os.environ.get("REX_BLOCK")
    if not is_macos() or not auto_title_enabled() or not block_id:
        return
    rex_bin = find_rex_bin()
    if not rex_bin:
        return
    info = inspect_session_for_block(rex_bin, block_id)
    session_id = info.get("session_id") if info else None
    if not info or not session_id or (expected_session_id and session_id != expected_session_id):
        return
    window, tiled_count = locate_window_for_block(info, block_id)
    if not window or not window.get("window_id"):
        return
    tab_label = maybe_sync_initial_session_label(rex_bin, info, tiled_count, cwd)

    window_id = window["window_id"]
    if state_path("win-agent", window_id).exists():
        return
    current = window.get("label")
    if not isinstance(current, str):
        return
    cache = state_path("win-shell", window_id)
    previous = read_state(cache)
    if current != previous and not DEFAULT_WINDOW_LABEL.fullmatch(current):
        return

    if tab_label is None:
        _, tab_label = resolve_dir_labels(cwd)
    tab_label = tab_label.strip()
    if tab_label and current != tab_label:
        run_rex(rex_bin, ["window", "rename", "-s", session_id, window_id, "--", tab_label])
        write_state(cache, tab_label)


def read_hook_event() -> dict:
    try:
        event = json.load(sys.stdin)
    except (OSError, ValueError):
        return {}
    return event if isinstance(event, dict) else {}


def start_background_sync(event: dict, retries: int) -> None:
    conversation_id = event.get("conversationId")
    cwd = event.get("cwd")
    if not isinstance(conversation_id, str):
        conversation_id = ""
    if not isinstance(cwd, str):
        cwd = ""
    if not cwd:
        paths = event.get("workspacePaths")
        if isinstance(paths, list) and paths and isinstance(paths[0], str):
            cwd = paths[0]
    if not conversation_id and not cwd:
        return

    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "agy-bg-sync",
        f"--conversation-id={conversation_id}",
        f"--retries={retries}",
    ]
    if cwd:
        command.append(f"--cwd={cwd}")
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def agy_hook(command: str) -> dict:
    response = {"decision": "stop"} if command == "agy-stop" else {}
    if not is_macos() or not os.environ.get("REX_BLOCK"):
        return response

    event = read_hook_event()
    if auto_title_enabled():
        try:
            start_background_sync(event, retries=5 if command == "agy-stop" else 0)
        except (OSError, ValueError):
            pass
    if command != "agy-session-start":
        return response

    rex_bin = find_rex_bin()
    if rex_bin:
        try:
            text = run_rex_text(
                rex_bin,
                ["__internal", "session-start", "--agent", "agy", "--feature-session-naming=false"],
                timeout=2.0,
            ).strip()
            if text:
                response["injectSteps"] = [{"systemMessage": {"systemMessage": text}}]
        except REX_ERRORS:
            pass
    return response


def print_zsh_hook() -> None:
    script = shlex.quote(str(Path(__file__).resolve()))
    print(f"""autoload -Uz add-zsh-hook
_rex_handoff_chpwd() {{
  [[ -n ${{REX_BLOCK:-}} ]] || return 0
  python3 {script} shell-cwd "$PWD" </dev/null >/dev/null 2>&1 &!
}}
add-zsh-hook chpwd _rex_handoff_chpwd
_rex_handoff_chpwd""")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synchronize Rex session and tab titles.")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("agy-session-start", "agy-pre-invocation", "agy-stop", "print-zsh-hook"):
        commands.add_parser(command)
    commands.add_parser("shell-cwd").add_argument("cwd", nargs="?", metavar="pwd")
    background = commands.add_parser("agy-bg-sync", help=argparse.SUPPRESS)
    background.add_argument("--conversation-id", required=True)
    background.add_argument("--cwd")
    background.add_argument("--retries", type=int, choices=range(len(TITLE_DELAYS)), default=0)
    args = parser.parse_args(argv)

    if args.command == "print-zsh-hook":
        print_zsh_hook()
    elif args.command == "shell-cwd":
        try:
            sync_shell_cwd(args.cwd or os.getcwd())
        except REX_ERRORS:
            pass
    elif args.command == "agy-bg-sync":
        try:
            sync_agy_title_bg(args.conversation_id, args.cwd, args.retries)
        except REX_ERRORS:
            pass
    else:
        print(json.dumps(agy_hook(args.command), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

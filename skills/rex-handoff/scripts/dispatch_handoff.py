#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rex_common import (
    REX_ERRORS,
    find_rex_bin,
    get_block_process,
    get_block_size,
    inspect_session_for_block,
    is_macos,
    iter_window_blocks,
    locate_window_for_block,
    run_rex,
    run_rex_json,
)


SHELL_NAMES = {"zsh", "-zsh", "bash", "-bash", "fish", "-fish", "sh", "-sh", "login"}
PROCESS_NAME_FIELDS = ("name", "argv0", "invoked_path", "path", "script")
SPLIT_DIRECTIONS = ("auto", "below", "right", "above", "left")


class DispatchError(RuntimeError):
    pass


def process_names(process: dict | None) -> set[str]:
    if not process:
        return set()
    return {
        os.path.basename(value)
        for field in PROCESS_NAME_FIELDS
        if (value := process.get(field))
    }


def is_omp_running(proc_info: dict) -> bool:
    return any(
        "omp" in process_names(process)
        for process in (
            proc_info.get("foreground"),
            *(proc_info.get("ancestors") or ()),
            proc_info.get("child"),
        )
    )


def is_idle_shell(proc_info: dict) -> bool:
    return (
        not proc_info.get("last_exit")
        and len(proc_info.get("ancestors") or ()) <= 1
        and bool(process_names(proc_info.get("foreground")) & SHELL_NAMES)
        and not is_omp_running(proc_info)
    )


def choose_split_direction(size_info: dict, min_wide_cols: int = 150) -> tuple[str, dict]:
    cols = size_info.get("cols", size_info.get("columns", 0))
    rows = size_info.get("rows", 0)
    cell_width = max(size_info.get("cell_width_px", 1), 1)
    cell_height = max(size_info.get("cell_height_px", 1), 1)
    width_px = cols * cell_width
    height_px = rows * cell_height
    direction = "below" if height_px >= width_px or cols < min_wide_cols else "right"
    return direction, {
        "cols": cols,
        "rows": rows,
        "cell_width_px": cell_width,
        "cell_height_px": cell_height,
        "width_px": width_px,
        "height_px": height_px,
    }


def build_prompt(
    handoff_path: str,
    result_path: str,
    scale: str = "simple",
    lang: str | None = None,
    model_tag: str | None = None,
) -> str:
    if lang is None:
        lang = os.environ.get("REX_HANDOFF_LANG", "zh")
    if lang not in {"zh", "en"}:
        raise ValueError("Language must be zh or en")
    if scale not in {"simple", "orchestrate"}:
        raise ValueError("Scale must be simple or orchestrate")

    if scale == "orchestrate":
        if lang == "zh":
            return (
                f"orchestrate 请先读取 {handoff_path}（如有切分支等前置步骤先在主工作区完成），"
                "按文档拆分通过 task 并行调度子 Agent 完成各切片修改；"
                f"全部子 Agent 完成后由你统一运行验证命令，并将汇总摘要写入 {result_path}"
            )
        return (
            f"orchestrate Read {handoff_path} first (complete any branch/setup prerequisites in the main "
            "workspace first), then decompose and dispatch parallel task subagents per slice as specified; "
            f"once all subagents finish, run the verification commands yourself and write a concise summary to {result_path}"
        )

    tag = (model_tag or "").strip()
    if tag.lower() in {"none", "inline"}:
        if lang == "zh":
            return f"请读取 {handoff_path} 完成实施与验证，完成后将变更与验证摘要写入 {result_path}"
        return (
            f"Read {handoff_path}, complete the implementation and verification, and write a concise change "
            f"and verification summary to {result_path}"
        )

    clean_tag = tag if tag.startswith("^") else f"^{tag}"
    if lang == "zh":
        agent = f"{clean_tag} 子 Agent" if tag else "task 子 Agent"
        return (
            f"请先读取 {handoff_path}（如有切分支等前置步骤先在主工作区完成），"
            f"然后通过 task 工具将代码与单测修改委派给 {agent} 执行（将文档中的修改要求与项目约束摘入 context）；"
            f"子 Agent 完成后由你按文档运行验证命令，并将变更与验证摘要写入 {result_path}"
        )
    agent = f"the {clean_tag} subagent" if tag else "a task subagent"
    return (
        f"Read {handoff_path} first (complete any branch/setup prerequisites in the main workspace first), "
        f"then delegate the code and unit-test changes to {agent} via the task tool (pass the file's requirements "
        "and repo constraints in context); once the subagent finishes, run the verification commands yourself "
        f"and write a concise summary to {result_path}"
    )


def has_omp() -> bool:
    if shutil.which("omp"):
        return True
    shell = os.environ.get("SHELL")
    if not shell:
        return False
    try:
        result = subprocess.run(
            [shell, "-ic", "command -v omp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=3.0,
        )
        return result.returncode == 0
    except REX_ERRORS:
        return False


def selected_agy_model(home: Path) -> str | None:
    for settings in (
        home / ".gemini/antigravity-cli/settings.json",
        home / ".gemini/config/settings.json",
    ):
        try:
            data = json.loads(settings.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("model"), str) and data["model"]:
            return data["model"]
    return None


def rex_agent_naming_enabled() -> bool:
    try:
        result = subprocess.run(
            ["defaults", "read", "com.superlogical.rex", "rex.agents.names-sessions"],
            check=True,
            capture_output=True,
            text=True,
            timeout=1.5,
        )
    except REX_ERRORS:
        return False
    return result.stdout.strip().lower() in {"1", "true", "yes"}


def doctor() -> dict:
    macos = is_macos()
    rex_bin = find_rex_bin()
    rex_session = os.environ.get("REX_SESSION") or None
    block_id = os.environ.get("REX_BLOCK")
    identity_session = None
    inside = False
    if rex_bin and block_id:
        try:
            identity = run_rex_json(rex_bin, ["--autostart=false", "whoami"], timeout=2.0)
        except REX_ERRORS:
            pass
        else:
            identity_session = identity.get("session_id") or None
            inside = (
                bool(identity_session)
                and identity.get("block_id") == block_id
                and (not rex_session or identity_session == rex_session)
            )
    omp_available = has_omp()
    home = Path.home()
    agent_dir = Path(os.environ.get("PI_CODING_AGENT_DIR") or home / ".omp/agent").expanduser()
    model = selected_agy_model(home)

    warnings = []
    if not macos:
        warnings.append("Rex Handoff requires macOS for live dispatch and title sync.")
    if not rex_bin:
        warnings.append("Rex CLI was not found. Open Rex or set REX_BIN_DIR to its bin directory.")
    if not inside:
        warnings.append("Start agy in a live Rex pane with a matching REX_BLOCK value.")
    if not omp_available:
        warnings.append("omp was not found in PATH or the interactive $SHELL.")
    if model and "orchestrator" in model.lower():
        warnings.append("Select a single model in agy using /model. omp handles orchestration.")
    if macos and rex_agent_naming_enabled():
        warnings.append("Turn off Rex Agent Session Naming (rex.agents.names-sessions) for directory-based session titles.")

    return {
        "ok": bool(macos and rex_bin and inside and omp_available),
        "checks": {
            "macos": macos,
            "rex_bin": rex_bin,
            "rex_session": rex_session or identity_session,
            "inside_rex_session": inside,
            "omp_available": omp_available,
            "omp_config_path": str(agent_dir / "config.yml"),
            "agy_selected_model": model,
        },
        "warnings": warnings,
    }


def wait_for_omp(rex_bin: str, session_id: str, block_id: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            process = get_block_process(rex_bin, session_id, block_id, timeout=min(1.0, remaining))
        except REX_ERRORS:
            process = {}
        if is_omp_running(process):
            return
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.25, remaining))
    raise DispatchError(f"omp did not start in Rex pane {block_id}. Check that pane for startup errors.")


def dispatch_handoff(cwd: str, prompt: str, *, direction: str = "auto", auto_enter: bool = False) -> dict:
    if direction not in SPLIT_DIRECTIONS:
        raise ValueError(f"Invalid split direction {direction!r}")
    if not is_macos():
        raise DispatchError("Live dispatch requires macOS")
    source_id = os.environ.get("REX_BLOCK")
    if not source_id:
        raise DispatchError("Run this command inside a Rex terminal (REX_BLOCK is required)")
    expected_session_id = os.environ.get("REX_SESSION") or None
    rex_bin = find_rex_bin()
    if not rex_bin:
        raise DispatchError("Rex CLI was not found. Run --doctor for details.")

    info = inspect_session_for_block(rex_bin, source_id)
    session_id = info.get("session_id") if info else None
    if not info or not session_id:
        raise DispatchError(f"Rex could not resolve the session containing pane {source_id}")
    if expected_session_id and session_id != expected_session_id:
        raise DispatchError(
            f"REX_SESSION mismatch for Rex pane {source_id} (expected {expected_session_id}, found {session_id})"
        )
    window, _ = locate_window_for_block(info, source_id)
    if window is None:
        raise DispatchError(f"Rex could not find the tab containing pane {source_id}")

    target_cwd = Path(os.path.realpath(cwd))
    first_running = None
    running = None
    idle = None
    for block in iter_window_blocks(window):
        block_id = block.get("block_id")
        if not block_id or block_id == source_id:
            continue
        if block.get("creator_name") not in {None, "com.superlogical.terminal"}:
            continue
        try:
            process = get_block_process(rex_bin, session_id, block_id)
        except REX_ERRORS:
            continue
        if is_omp_running(process):
            first_running = first_running or block_id
            process_cwd = (process.get("foreground") or {}).get("cwd")
            if not process_cwd:
                continue
            process_dir = Path(os.path.realpath(process_cwd))
            if process_dir.is_relative_to(target_cwd) or target_cwd.is_relative_to(process_dir):
                running = block_id
                break
        elif idle is None and is_idle_shell(process):
            idle = block_id

    running = running or first_running
    launch = None
    split_direction = None
    size = None
    if running:
        target_id = running
        method = "reuse-omp"
    elif idle:
        target_id = idle
        method = "reuse-shell"
        launch = f"cd {shlex.quote(cwd)} && omp"
    else:
        if direction == "auto":
            split_direction, size = choose_split_direction(get_block_size(rex_bin, session_id, source_id))
        else:
            split_direction = direction
        split = run_rex_json(
            rex_bin,
            [
                "split", "-s", session_id, "-b", source_id, f"--split={split_direction}",
                "-c", cwd, "--label", "omp", "--focus=false",
            ],
        )
        block_ids = split.get("block_ids") or ()
        if not block_ids or not block_ids[0] or block_ids[0] == source_id:
            raise DispatchError("Rex did not return the new pane ID")
        target_id = block_ids[0]
        method = "split"
        time.sleep(0.5)
        launch = "omp"

    target = ["-s", session_id, "-b", target_id]
    if launch:
        run_rex(rex_bin, ["send", *target, "--", launch])
        run_rex(rex_bin, ["send-key", *target, "enter"])
        wait_for_omp(rex_bin, session_id, target_id)

    run_rex(rex_bin, ["send", *target, "--", prompt])
    if auto_enter:
        run_rex(rex_bin, ["send-key", *target, "enter"])

    return {
        "session_id": session_id,
        "block_id": target_id,
        "method": method,
        "split_direction": split_direction,
        "source_size": size,
        "auto_enter": auto_enter,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hand an agy implementation plan over to omp in Rex.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--doctor", action="store_true", help="Report local environment checks as JSON")
    modes.add_argument("--dry-run", action="store_true", help="Print the prompt without contacting Rex")
    parser.add_argument("--handoff", help="Path to the handoff document")
    parser.add_argument("--result", help="Path for the omp result summary")
    parser.add_argument("--cwd", default=os.getcwd(), help="Workspace directory (default: current directory)")
    parser.add_argument("--scale", choices=("simple", "orchestrate"), default="simple")
    parser.add_argument("--lang", choices=("zh", "en"), help="Prompt language (default: REX_HANDOFF_LANG or zh)")
    parser.add_argument("--model-tag", help="Custom omp task model tag, or none/inline for direct execution")
    parser.add_argument(
        "--direction", choices=SPLIT_DIRECTIONS, default="auto",
        help="Direction for a new pane. By default, auto chooses below or right based on pane size and column count",
    )
    parser.add_argument("--auto-enter", action="store_true", help="Submit the prompt after sending it")
    args = parser.parse_args(argv)

    if args.doctor:
        print(json.dumps(doctor(), ensure_ascii=False, indent=2))
        return 0
    if not args.handoff or not args.result:
        parser.error("--handoff and --result are required unless --doctor is used")

    try:
        cwd = str(Path(args.cwd).expanduser().resolve())
        handoff = Path(args.handoff).expanduser().absolute()
        result = Path(args.result).expanduser().absolute()
        prompt = build_prompt(str(handoff), str(result), args.scale, args.lang, args.model_tag)
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    if any(ord(character) < 32 or ord(character) == 127 for character in prompt + cwd):
        parser.error("Paths and model tags cannot contain terminal control characters")

    if args.dry_run:
        data = {
            "dry_run": True,
            "cwd": cwd,
            "scale": args.scale,
            "direction": args.direction,
            "auto_enter": args.auto_enter,
            "prompt": prompt,
        }
    else:
        if not handoff.is_file():
            parser.error(f"Handoff file not found: {handoff}")
        if not Path(cwd).is_dir():
            parser.error(f"Workspace directory not found: {cwd}")
        try:
            data = dispatch_handoff(cwd, prompt, direction=args.direction, auto_enter=args.auto_enter)
        except (DispatchError, *REX_ERRORS) as error:
            message = str(error)
            if isinstance(error, subprocess.CalledProcessError) and error.stderr:
                message = error.stderr.strip()
            parser.exit(1, f"rex-handoff: {message}\n")

    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

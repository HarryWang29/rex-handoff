#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import call, patch


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/rex-handoff/scripts"
sys.path.insert(0, str(SCRIPTS))

import dispatch_handoff as dispatch
import rex_auto_title as titles
import rex_common as rex


REX_BLOCK_ENV = {"REX_BLOCK": "source-id"}
REX_ENV = {**REX_BLOCK_ENV, "REX_SESSION": "session-id"}
SOURCE = {"block_id": "source-id", "creator_name": "com.superlogical.terminal"}
PEER = {"block_id": "peer-id", "creator_name": "com.superlogical.terminal"}
IDLE = {"foreground": {"name": "zsh"}, "ancestors": [{"name": "login"}]}
OMP = {
    "foreground": {
        "name": "19.1.1",
        "path": "/opt/omp/19.1.1",
        "invoked_path": "/home/developer/.local/bin/omp",
        "argv0": "omp",
    },
    "ancestors": [{"name": "zsh"}, {"name": "login"}],
    "child": {"name": "login"},
}


def rex_session(*extra_blocks: dict, label: str = "quiet-meadow", window_label: str = "Window 4ea630") -> dict:
    return {
        "session_id": REX_ENV["REX_SESSION"],
        "label": label,
        "windows": [
            {
                "window_id": "window-id",
                "label": window_label,
                "focused_block_id": REX_ENV["REX_BLOCK"],
                "layers": [{"kind": "tiled", "blocks": [SOURCE, *extra_blocks]}],
            }
        ],
    }


class MockedTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)


class FindRexTests(MockedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = Path("/home/developer")
        self.macos = self.contexts.enter_context(patch.object(rex, "is_macos", return_value=True))
        self.which = self.contexts.enter_context(patch.object(rex.shutil, "which", return_value=None))
        self.executable = self.contexts.enter_context(patch.object(rex, "executable_path", return_value=None))
        self.spotlight = self.contexts.enter_context(patch.object(rex.subprocess, "run"))
        self.contexts.enter_context(patch.object(Path, "home", return_value=self.home))

    def test_non_macos_returns_without_probing(self) -> None:
        self.macos.return_value = False
        self.assertIsNone(rex.find_rex_bin({"REX_BIN_DIR": "/injected/bin"}))
        self.executable.assert_not_called()
        self.which.assert_not_called()
        self.spotlight.assert_not_called()

    def test_injected_directory_precedes_path(self) -> None:
        self.executable.return_value = "/injected/bin/rex"
        self.which.return_value = "/from-path/rex"
        self.assertEqual(rex.find_rex_bin({"REX_BIN_DIR": "/injected/bin", "PATH": "/from-path"}), "/injected/bin/rex")
        self.executable.assert_called_once_with(Path("/injected/bin/rex"))
        self.which.assert_not_called()

    def test_path_precedes_current_server_after_invalid_injected_directory(self) -> None:
        self.which.return_value = "/from-path/rex"
        with patch.dict(os.environ, {"REX_BIN_DIR": "/missing/bin", "PATH": "/from-path"}, clear=True):
            self.assertEqual(rex.find_rex_bin(), "/from-path/rex")
        self.executable.assert_called_once_with(Path("/missing/bin/rex"))
        self.which.assert_called_once_with("rex", path="/from-path")
        self.spotlight.assert_not_called()

    def test_current_server_precedes_app_bundles(self) -> None:
        current = self.home / (
            "Library/Application Support/com.superlogical.rex/"
            "RexServer/Versions/Current/Rex Server.app/Contents/MacOS/rex"
        )
        self.executable.side_effect = lambda path: str(path) if path == current else None
        self.assertEqual(rex.find_rex_bin({}), str(current))
        self.executable.assert_called_once_with(current)
        self.spotlight.assert_not_called()

    def test_app_order_is_standard_user_then_beta(self) -> None:
        apps = [
            Path("/Applications/Rex.app"),
            self.home / "Applications/Rex.app",
            Path("/Applications/Rex Beta.app"),
            self.home / "Applications/Rex Beta.app",
        ]
        binaries = [app / rex.APP_REX_PATH for app in apps]
        for index, binary in enumerate(binaries):
            with self.subTest(binary=binary):
                self.executable.reset_mock()
                self.executable.side_effect = lambda path: str(path) if path == binary else None
                self.assertEqual(rex.find_rex_bin({}), str(binary))
                self.assertEqual(self.executable.call_args_list[1:], [call(p) for p in binaries[:index + 1]])
        self.spotlight.assert_not_called()

    def test_spotlight_uses_first_executable_app_and_bounded_timeout(self) -> None:
        binary = Path("/Custom Folder/Rex.app") / rex.APP_REX_PATH
        self.executable.side_effect = lambda path: str(path) if path == binary else None
        self.spotlight.return_value.stdout = "/skip/not-an-app\n/skip/Old Rex.app\n/Custom Folder/Rex.app\n"
        self.assertEqual(rex.find_rex_bin({}), str(binary))
        self.assertEqual(
            self.executable.call_args_list[-2:],
            [call(Path("/skip/Old Rex.app") / rex.APP_REX_PATH), call(binary)],
        )
        self.spotlight.assert_called_once_with(
            ["mdfind", "kMDItemCFBundleIdentifier == 'com.superlogical.rex'"],
            check=True, capture_output=True, text=True, timeout=1.5,
        )

    def test_spotlight_timeout_returns_unavailable(self) -> None:
        self.spotlight.side_effect = subprocess.TimeoutExpired("mdfind", 1.5)
        self.assertIsNone(rex.find_rex_bin({}))


class RexCliTests(unittest.TestCase):
    def test_json_mode_is_used_for_inspect_and_not_used_for_block_calls(self) -> None:
        with patch.object(rex, "run_rex_text", return_value='{"ok": true}') as run:
            self.assertEqual(rex.run_rex_json("rex", ["session", "inspect"]), {"ok": True})
            self.assertEqual(rex.get_block_process("rex", "s", "b"), {"ok": True})
            self.assertEqual(rex.get_block_size("rex", "s", "b"), {"ok": True})
            rex.run_rex("rex", ["send", "hello"], timeout=2.0)
        self.assertEqual(run.call_args_list, [
            call("rex", ["session", "inspect", "--json"], 5.0),
            call("rex", ["block", "call", "-s", "s", "-b", "b", "process"], 5.0),
            call("rex", ["block", "call", "-s", "s", "-b", "b", "size"], 5.0),
            call("rex", ["send", "hello"], 2.0),
        ])

    def test_session_inspect_uses_current_session_without_rex_session(self) -> None:
        target = rex_session()
        with patch.dict(os.environ, REX_BLOCK_ENV, clear=True), patch.object(
            rex, "run_rex_json", return_value=target,
        ) as run:
            self.assertEqual(rex.inspect_session_for_block("rex", "source-id"), target)
        run.assert_called_once_with("rex", ["session", "inspect"])

    def test_session_inspect_falls_back_to_environment_session(self) -> None:
        target = rex_session()
        unrelated = {"session_id": "unrelated", "windows": []}
        with patch.dict(os.environ, REX_ENV, clear=True), patch.object(
            rex, "run_rex_json", side_effect=[unrelated, target],
        ) as run:
            self.assertEqual(rex.inspect_session_for_block("rex", "source-id"), target)
        self.assertEqual(run.call_args_list, [
            call("rex", ["session", "inspect"]),
            call("rex", ["session", "inspect", "session-id"]),
        ])

    def test_session_inspect_uses_whoami_after_unrelated_default_session(self) -> None:
        target = rex_session()
        unrelated = {
            "session_id": "unrelated",
            "windows": [{"window_id": "other-window", "layers": [{"kind": "tiled", "blocks": [PEER]}]}],
        }
        identity = {"session_id": "session-id", "block_id": "source-id"}
        with patch.dict(os.environ, REX_BLOCK_ENV, clear=True), patch.object(
            rex, "run_rex_json", side_effect=[unrelated, identity, target],
        ) as run:
            self.assertEqual(rex.inspect_session_for_block("rex", "source-id"), target)
        self.assertEqual(run.call_args_list, [
            call("rex", ["session", "inspect"]),
            call("rex", ["--autostart=false", "whoami"], timeout=2.0),
            call("rex", ["session", "inspect", "session-id"]),
        ])

    def test_session_inspect_uses_whoami_after_inspect_errors(self) -> None:
        target = {**rex_session(), "session_id": "live-session"}
        identity = {"session_id": "live-session", "block_id": "source-id"}
        with patch.dict(os.environ, REX_ENV, clear=True), patch.object(
            rex, "run_rex_json", side_effect=[
                OSError("Rex unavailable"), subprocess.CalledProcessError(1, "rex"), identity, target,
            ],
        ) as run:
            self.assertEqual(rex.inspect_session_for_block("rex", "source-id"), target)
        self.assertEqual(run.call_args_list, [
            call("rex", ["session", "inspect"]),
            call("rex", ["session", "inspect", "session-id"]),
            call("rex", ["--autostart=false", "whoami"], timeout=2.0),
            call("rex", ["session", "inspect", "live-session"]),
        ])

    def test_session_inspect_skips_whoami_session_already_tried(self) -> None:
        unrelated = {"session_id": "unrelated", "windows": []}
        missing_pane = {"session_id": "session-id", "windows": []}
        identity = {"session_id": "session-id", "block_id": "peer-id"}
        with patch.dict(os.environ, REX_ENV, clear=True), patch.object(
            rex, "run_rex_json", side_effect=[unrelated, missing_pane, identity],
        ) as run:
            self.assertIsNone(rex.inspect_session_for_block("rex", "source-id"))
        self.assertEqual(run.call_args_list, [
            call("rex", ["session", "inspect"]),
            call("rex", ["session", "inspect", "session-id"]),
            call("rex", ["--autostart=false", "whoami"], timeout=2.0),
        ])

    def test_session_inspect_returns_none_when_whoami_cannot_resolve_session(self) -> None:
        unrelated = {"session_id": "unrelated", "windows": []}
        for identity in ({"block_id": "source-id"}, subprocess.CalledProcessError(1, "rex")):
            with self.subTest(identity=identity), patch.dict(os.environ, REX_BLOCK_ENV, clear=True), patch.object(
                rex, "run_rex_json", side_effect=[unrelated, identity],
            ) as run:
                self.assertIsNone(rex.inspect_session_for_block("rex", "source-id"))
                self.assertEqual(run.call_args_list, [
                    call("rex", ["session", "inspect"]),
                    call("rex", ["--autostart=false", "whoami"], timeout=2.0),
                ])

    def test_foreign_session_is_never_returned_when_pane_is_missing(self) -> None:
        unrelated = rex_session()
        identity = {"session_id": "session-id", "block_id": "source-id"}
        for info in (unrelated, OSError("Rex unavailable")):
            with self.subTest(info=info), patch.dict(os.environ, {"REX_BLOCK": "missing"}, clear=True), patch.object(
                rex, "run_rex_json", side_effect=[unrelated, identity, info],
            ) as run:
                self.assertIsNone(rex.inspect_session_for_block("rex", "missing"))
                self.assertEqual(run.call_args_list, [
                    call("rex", ["session", "inspect"]),
                    call("rex", ["--autostart=false", "whoami"], timeout=2.0),
                    call("rex", ["session", "inspect", "session-id"]),
                ])

    def test_locate_counts_only_tiled_panes_and_can_find_unfocused_or_floating_pane(self) -> None:
        info = rex_session(PEER)
        window = info["windows"][0]
        window["layers"].append({"kind": "floating", "blocks": [{"block_id": "float-id"}]})
        for block in ("source-id", "peer-id", "float-id"):
            with self.subTest(block=block):
                self.assertEqual(rex.locate_window_for_block(info, block), (window, 2))
        self.assertEqual(rex.locate_window_for_block(info, "missing"), (None, 0))


class TitleParsingTests(unittest.TestCase):
    def test_git_root_subdirectory_and_linked_worktree_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "my-app"
            subdir = root / "src"
            worktree = root.with_name("my-app-user-auth")
            worktree_subdir = worktree / "src"
            subdir.mkdir(parents=True)
            worktree_subdir.mkdir(parents=True)
            for directory, top_level in ((root, root), (subdir, root), (worktree_subdir, worktree)):
                with self.subTest(directory=directory), patch.object(titles.subprocess, "run") as git:
                    git.return_value.stdout = f"{top_level}\n"
                    self.assertEqual(titles.resolve_dir_labels(str(directory)), (top_level.name,) * 2)
                    git.assert_called_once_with(
                        ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
                        check=True, capture_output=True, encoding="utf-8", timeout=1.5,
                    )

    def test_non_repository_home_root_and_temporary_labels(self) -> None:
        cases = (
            ("/home/developer/", (None, "~")),
            ("/", (None, "/")),
            ("/tmp", (None, "tmp")),
            ("/private/tmp/demo", (None, "demo")),
            ("/var/folders/project", (None, "project")),
            ("/private/var/folders/work", (None, "work")),
            ("/tmp-project/my-app", ("my-app", "my-app")),
        )
        with patch.object(titles.subprocess, "run", side_effect=subprocess.CalledProcessError(128, "git")):
            for directory, result in cases:
                with self.subTest(directory=directory):
                    self.assertEqual(titles.resolve_dir_labels(directory, "/home/developer"), result)

    def test_pbtxt_decodes_octal_utf8_without_corrupting_literal_unicode(self) -> None:
        self.assertEqual(
            titles.decode_pbtxt_string(r'\345\244\247\345\256\266说\"hi\"\\'),
            '大家说"hi"\\',
        )
        self.assertEqual(titles.decode_pbtxt_string(r'\x41\tB'), "A\tB")

    def test_annotation_title_precedes_metadata_and_invalid_annotation_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            agy = Path(temporary)
            (agy / "annotations").mkdir()
            (agy / "cache").mkdir()
            file = agy / "annotations/conversation-id.pbtxt"
            (agy / "cache/conversation_metadata.json").write_text(json.dumps({
                "conversations": {"conversation-id": {"summary": {"Title": "Cached title"}}},
            }), encoding="utf-8")

            file.write_text('other_title: "ignore"\ntitle: "\\345\\244\\247家 \\"hello\\""\n', encoding="utf-8")
            self.assertEqual(titles.read_agy_title("conversation-id", agy), '大家 "hello"')
            file.write_text(r'title: "\377"', encoding="utf-8")
            self.assertEqual(titles.read_agy_title("conversation-id", agy), "Cached title")
            file.unlink()
            self.assertEqual(titles.read_agy_title("conversation-id", agy), "Cached title")
            self.assertEqual(titles.read_agy_title("unknown-id", agy), "")
            self.assertEqual(titles.read_agy_title("../conversation-id", agy), "")


class TitleSyncTests(MockedTestCase):
    def setUp(self) -> None:
        super().setUp()
        temporary = self.contexts.enter_context(tempfile.TemporaryDirectory())
        self.state = Path(temporary) / "private-state"
        self.contexts.enter_context(patch.object(titles, "state_directory", return_value=self.state))
        self.contexts.enter_context(patch.object(titles, "is_macos", return_value=True))
        self.contexts.enter_context(patch.object(titles, "find_rex_bin", return_value="rex"))
        self.contexts.enter_context(patch.dict(os.environ, REX_ENV, clear=True))
        self.rename = self.contexts.enter_context(patch.object(titles, "run_rex"))
        self.labels = self.contexts.enter_context(
            patch.object(titles, "resolve_dir_labels", return_value=("my-app", "my-app"))
        )

    def test_session_tracks_automatic_label_and_preserves_user_rename(self) -> None:
        info = rex_session()
        self.assertEqual(titles.maybe_sync_initial_session_label("rex", info, 1, "/projects/my-app"), "my-app")
        self.rename.assert_called_once_with("rex", ["session", "rename", "session-id", "--", "my-app"])
        self.assertEqual((self.state / "session-session-id.txt").read_text(encoding="utf-8"), "my-app")

        info["label"] = "my-app"
        self.labels.return_value = ("other-app", "other-app")
        self.rename.reset_mock()
        titles.maybe_sync_initial_session_label("rex", info, 1, "/projects/other-app")
        self.rename.assert_called_once_with("rex", ["session", "rename", "session-id", "--", "other-app"])

        for current in ("My session", "manual-label", "quiet-meadow"):
            with self.subTest(current=current):
                info["label"] = current
                self.rename.reset_mock()
                titles.maybe_sync_initial_session_label("rex", info, 1, "/projects/other-app")
                self.rename.assert_not_called()

    def test_session_preserves_custom_labels_splits_and_multiple_tabs(self) -> None:
        multiple_tabs = rex_session()
        multiple_tabs["windows"].append({"window_id": "next-window"})
        for info, count in ((rex_session(PEER), 2), (multiple_tabs, 1)):
            with self.subTest(info=info, count=count):
                self.assertIsNone(titles.maybe_sync_initial_session_label("rex", info, count, "/projects/my-app"))
        for label in ("My session", "Quiet-Meadow", "my-app-2", "my_app", "my-app-dev"):
            with self.subTest(label=label):
                titles.maybe_sync_initial_session_label("rex", rex_session(label=label), 1, "/projects/my-app")
        self.rename.assert_not_called()

    def test_agent_retries_title_and_blocks_subsequent_shell_rename(self) -> None:
        info = rex_session(PEER)
        with patch.dict(os.environ, REX_BLOCK_ENV, clear=True), patch.object(
            rex, "run_rex_json", return_value=info,
        ), patch.object(
            titles, "read_agy_title", side_effect=["", "", "Implement user auth"],
        ) as read, patch.object(titles.time, "sleep") as sleep:
            titles.sync_agy_title_bg("my-conversation", "/projects/my-app", retries=5)
            self.assertEqual(read.call_args_list, [call("my-conversation")] * 3)
            self.assertEqual(sleep.call_args_list, [call(1.0), call(1.5)])
            titles.sync_shell_cwd("/projects/my-app")
        self.rename.assert_called_once_with(
            "rex", ["window", "rename", "-s", "session-id", "window-id", "--", "Implement user auth"]
        )
        self.assertEqual((self.state / "win-agent-window-id.txt").read_text(encoding="utf-8"), "source-id")
        self.assertEqual((self.state / "block-agy-source-id.txt").read_text(encoding="utf-8"), "Implement user auth")

    def test_agent_sync_can_name_initial_session_without_title_or_shell_hook(self) -> None:
        with patch.dict(os.environ, REX_BLOCK_ENV, clear=True), patch.object(
            rex, "run_rex_json", return_value=rex_session(label="bright-river"),
        ), patch.object(titles, "read_agy_title", return_value=""):
            titles.sync_agy_title_bg("my-conversation", "/projects/my-app")
        self.rename.assert_called_once_with("rex", ["session", "rename", "session-id", "--", "my-app"])

    def test_same_agent_title_does_not_trigger_rename_but_retry_can_pick_up_change(self) -> None:
        titles.write_state(titles.state_path("block-agy", "source-id"), "Existing title")
        info = rex_session(label="manual-label", window_label="Existing title")
        with patch.object(titles, "inspect_session_for_block", return_value=info), patch.object(
            titles, "read_agy_title", side_effect=["Existing title", "Existing title", "New title"],
        ), patch.object(titles.time, "sleep", return_value=None):
            titles.sync_agy_title_bg("my-conversation")
            self.rename.assert_not_called()
            titles.sync_agy_title_bg("my-conversation", retries=2)
        self.rename.assert_called_once_with("rex", ["window", "rename", "-s", "session-id", "window-id", "--", "New title"])

    def test_shell_tab_uses_default_or_previous_label_and_preserves_custom_label(self) -> None:
        with patch.dict(os.environ, REX_BLOCK_ENV, clear=True), patch.object(rex, "run_rex_json") as inspect:
            inspect.return_value = rex_session(label="My session")
            titles.sync_shell_cwd("/projects/my-app")
            self.labels.return_value = (None, "~")
            inspect.return_value = rex_session(label="My session", window_label="my-app")
            titles.sync_shell_cwd("/home/developer")
            inspect.return_value = rex_session(label="My session", window_label="My tab")
            titles.sync_shell_cwd("/projects/my-app")
        self.assertEqual(self.rename.call_args_list, [
            call("rex", ["window", "rename", "-s", "session-id", "window-id", "--", "my-app"]),
            call("rex", ["window", "rename", "-s", "session-id", "window-id", "--", "~"]),
        ])

    def test_title_sync_skips_unresolved_and_mismatched_sessions(self) -> None:
        cases = (
            (REX_BLOCK_ENV, None),
            (REX_BLOCK_ENV, {**rex_session(), "session_id": ""}),
            ({**REX_ENV, "REX_SESSION": "old-session"}, rex_session()),
        )
        for env, info in cases:
            with self.subTest(env=env, info=info), patch.dict(os.environ, env, clear=True), patch.object(
                titles, "inspect_session_for_block", return_value=info,
            ), patch.object(titles, "read_agy_title", return_value="Implement user auth"):
                titles.sync_agy_title_bg("my-conversation", "/projects/my-app")
                titles.sync_shell_cwd("/projects/my-app")
                self.rename.assert_not_called()
                self.assertEqual(list(self.state.glob("*.txt")), [])


class HookTests(unittest.TestCase):
    def run_hook(self, command: str, payload: str = "{}") -> dict:
        with patch.object(sys, "stdin", io.StringIO(payload)), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(titles.main([command]), 0)
        return json.loads(out.getvalue())

    def test_hooks_are_inert_without_rex_or_macos(self) -> None:
        for macos, env in ((True, {}), (True, {"REX_SESSION": "session-id"}), (False, REX_ENV)):
            with self.subTest(macos=macos, env=env), patch.dict(os.environ, env, clear=True), patch.object(
                titles, "is_macos", return_value=macos,
            ), patch.object(titles, "find_rex_bin") as find, patch.object(titles, "start_background_sync") as spawn:
                self.assertEqual(self.run_hook("agy-session-start"), {})
                self.assertEqual(self.run_hook("agy-pre-invocation"), {})
                self.assertEqual(self.run_hook("agy-stop"), {"decision": "stop"})
                find.assert_not_called()
                spawn.assert_not_called()

    def test_start_injects_context_even_with_auto_title_disabled(self) -> None:
        for setting in ("0", " FALSE ", "off"):
            with self.subTest(setting=setting), patch.dict(
                os.environ, {**REX_BLOCK_ENV, "REX_HANDOFF_AUTO_TITLE": setting}, clear=True,
            ), patch.object(titles, "is_macos", return_value=True), patch.object(
                titles, "find_rex_bin", return_value="rex",
            ), patch.object(titles, "run_rex_text", return_value="Context for Rex\n") as context, patch.object(
                titles, "start_background_sync",
            ) as spawn:
                self.assertEqual(self.run_hook("agy-session-start"), {
                    "injectSteps": [{"systemMessage": {"systemMessage": "Context for Rex"}}],
                })
                self.assertEqual(self.run_hook("agy-stop"), {"decision": "stop"})
                context.assert_called_once_with(
                    "rex", ["__internal", "session-start", "--agent", "agy", "--feature-session-naming=false"],
                    timeout=2.0,
                )
                spawn.assert_not_called()

    def test_hook_launches_detached_background_process_with_correct_retry_counts(self) -> None:
        event = json.dumps({"conversationId": "my-conversation", "workspacePaths": ["/projects/my-app"]})
        with patch.dict(os.environ, REX_BLOCK_ENV, clear=True), patch.object(
            titles, "is_macos", return_value=True,
        ), patch.object(titles, "find_rex_bin", return_value=None), patch.object(
            titles.subprocess, "Popen",
        ) as popen:
            self.assertEqual(self.run_hook("agy-session-start", event), {})
            self.assertEqual(self.run_hook("agy-pre-invocation", event), {})
            self.assertEqual(self.run_hook("agy-stop", event), {"decision": "stop"})
            self.assertEqual(self.run_hook("agy-stop", "malformed JSON"), {"decision": "stop"})
        self.assertEqual(popen.call_args_list, [
            call(
                [sys.executable, str(SCRIPTS / "rex_auto_title.py"), "agy-bg-sync",
                 "--conversation-id=my-conversation", f"--retries={retry}", "--cwd=/projects/my-app"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            for retry in (0, 0, 5)
        ])

    def test_print_zsh_hook_targets_absolute_script_and_initializes_current_directory(self) -> None:
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(titles.main(["print-zsh-hook"]), 0)
        hook = out.getvalue()
        self.assertIn("[[ -n ${REX_BLOCK:-} ]] || return 0", hook)
        self.assertNotIn("REX_SESSION", hook)
        self.assertIn(f"python3 {shlex.quote(str(SCRIPTS / 'rex_auto_title.py'))} shell-cwd \"$PWD\"", hook)
        self.assertIn("add-zsh-hook chpwd _rex_handoff_chpwd", hook)
        self.assertTrue(hook.endswith("\n_rex_handoff_chpwd\n"))


class DispatchLogicTests(unittest.TestCase):
    def test_running_omp_is_identified_in_foreground_ancestor_or_child(self) -> None:
        self.assertTrue(dispatch.is_omp_running(OMP))
        self.assertTrue(dispatch.is_omp_running({"foreground": {"invoked_path": "/tools/omp"}}))
        self.assertTrue(dispatch.is_omp_running({"foreground": {"argv0": "omp"}}))
        self.assertTrue(dispatch.is_omp_running({"ancestors": [{"name": "omp"}]}))
        self.assertTrue(dispatch.is_omp_running({"child": {"script": "/tools/omp"}}))
        self.assertFalse(dispatch.is_omp_running({"foreground": {"name": "omp-wrapper"}, "ancestors": None}))

    def test_idle_shells_accept_login_ancestors_but_exclude_running_jobs(self) -> None:
        for shell in dispatch.SHELL_NAMES:
            with self.subTest(shell=shell):
                self.assertTrue(dispatch.is_idle_shell({"foreground": {"argv0": shell}, "ancestors": None}))
        self.assertTrue(dispatch.is_idle_shell(IDLE))
        for process in (
            OMP, {}, {"child": {"name": "login"}},
            {"foreground": {"name": "vim"}, "ancestors": [{"name": "bash"}]},
            {"foreground": {"name": "bash"}, "ancestors": [{"name": "bash"}, {"name": "login"}]},
            {**IDLE, "last_exit": {"exit_code": 0}},
        ):
            with self.subTest(process=process):
                self.assertFalse(dispatch.is_idle_shell(process))

    def test_split_direction_requires_both_width_and_at_least_150_columns(self) -> None:
        cases = (
            ({"cols": 149, "rows": 30, "cell_width_px": 9, "cell_height_px": 16}, "below"),
            ({"columns": 150, "rows": 50, "cell_width_px": 8, "cell_height_px": 16}, "right"),
            ({"cols": 150, "rows": 75, "cell_width_px": 8, "cell_height_px": 16}, "below"),
            ({"cols": 150, "rows": 76, "cell_width_px": 8, "cell_height_px": 16}, "below"),
            ({"cols": 300, "rows": 80, "cell_width_px": 9, "cell_height_px": 18}, "right"),
        )
        for size, expected in cases:
            with self.subTest(size=size):
                direction, details = dispatch.choose_split_direction(size)
                self.assertEqual(direction, expected)
                self.assertEqual(details["width_px"], details["cols"] * size["cell_width_px"])
                self.assertEqual(details["height_px"], details["rows"] * size["cell_height_px"])
        direction, details = dispatch.choose_split_direction({
            "cols": 151, "rows": 44, "cell_width_px": 0, "cell_height_px": 0,
        }, min_wide_cols=152)
        self.assertEqual((direction, details["width_px"], details["height_px"]), ("below", 151, 44))

    def test_prompt_matches_chinese_and_english_templates_and_model_tag_modes(self) -> None:
        handoff = "/tmp/handoff-my-app.md"
        result = "/tmp/handoff-result-my-app.md"
        default_zh = (
            "请先读取 /tmp/handoff-my-app.md（如有切分支等前置步骤先在主工作区完成），"
            "然后通过 task 工具将代码与单测修改委派给 task 子 Agent 执行（将文档中的修改要求与项目约束摘入 context）；"
            "子 Agent 完成后由你按文档运行验证命令，并将变更与验证摘要写入 /tmp/handoff-result-my-app.md"
        )
        default_en = (
            "Read /tmp/handoff-my-app.md first (complete any branch/setup prerequisites in the main workspace first), "
            "then delegate the code and unit-test changes to a task subagent via the task tool (pass the file's requirements "
            "and repo constraints in context); once the subagent finishes, run the verification commands yourself and write "
            "a concise summary to /tmp/handoff-result-my-app.md"
        )
        orchestrate_zh = (
            "orchestrate 请先读取 /tmp/handoff-my-app.md（如有切分支等前置步骤先在主工作区完成），"
            "按文档拆分通过 task 并行调度子 Agent 完成各切片修改；"
            "全部子 Agent 完成后由你统一运行验证命令，并将汇总摘要写入 /tmp/handoff-result-my-app.md"
        )
        orchestrate_en = (
            "orchestrate Read /tmp/handoff-my-app.md first (complete any branch/setup prerequisites in the main workspace "
            "first), then decompose and dispatch parallel task subagents per slice as specified; once all subagents finish, "
            "run the verification commands yourself and write a concise summary to /tmp/handoff-result-my-app.md"
        )
        cases = (
            ("simple", "zh", None, default_zh),
            ("simple", "en", "", default_en),
            ("simple", "zh", "  model-a ", default_zh.replace("task 子 Agent", "^model-a 子 Agent")),
            ("simple", "en", "^model-a", default_en.replace("a task subagent", "the ^model-a subagent")),
            ("simple", "zh", " None ", "请读取 /tmp/handoff-my-app.md 完成实施与验证，完成后将变更与验证摘要写入 /tmp/handoff-result-my-app.md"),
            ("simple", "en", "INLINE", "Read /tmp/handoff-my-app.md, complete the implementation and verification, "
             "and write a concise change and verification summary to /tmp/handoff-result-my-app.md"),
            ("orchestrate", "zh", None, orchestrate_zh),
            ("orchestrate", "en", "^model-a", orchestrate_en),
        )
        for scale, language, model, expected in cases:
            with self.subTest(scale=scale, language=language, model=model):
                self.assertEqual(dispatch.build_prompt(handoff, result, scale, language, model), expected)
        with patch.dict(os.environ, {"REX_HANDOFF_LANG": "en"}, clear=True):
            self.assertEqual(dispatch.build_prompt(handoff, result), default_en)
            self.assertEqual(dispatch.build_prompt(handoff, result, lang="zh"), default_zh)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(dispatch.build_prompt(handoff, result), default_zh)


class DispatchRexTests(MockedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.contexts.enter_context(patch.dict(os.environ, REX_ENV, clear=True))
        self.contexts.enter_context(patch.object(dispatch, "is_macos", return_value=True))
        self.contexts.enter_context(patch.object(dispatch, "find_rex_bin", return_value="rex"))
        self.inspect = self.contexts.enter_context(patch.object(dispatch, "inspect_session_for_block"))
        self.process = self.contexts.enter_context(patch.object(dispatch, "get_block_process"))
        self.size = self.contexts.enter_context(patch.object(dispatch, "get_block_size"))
        self.split = self.contexts.enter_context(patch.object(dispatch, "run_rex_json"))
        self.send = self.contexts.enter_context(patch.object(dispatch, "run_rex"))
        self.wait = self.contexts.enter_context(patch.object(dispatch, "wait_for_omp"))
        self.sleep = self.contexts.enter_context(patch.object(dispatch.time, "sleep"))
        self.cwd = "/projects/my-app"
        self.prompt = "Read the handoff."

    def test_running_omp_takes_priority_over_idle_shell_and_prompt_is_prefilled(self) -> None:
        running = {"block_id": "omp-id", "creator_name": "com.superlogical.terminal"}
        self.inspect.return_value = rex_session(PEER, running)
        self.process.side_effect = [IDLE, OMP]
        result = dispatch.dispatch_handoff(self.cwd, self.prompt)
        self.assertEqual((result["method"], result["block_id"]), ("reuse-omp", "omp-id"))
        self.assertEqual(self.process.call_args_list, [
            call("rex", "session-id", "peer-id"), call("rex", "session-id", "omp-id"),
        ])
        self.send.assert_called_once_with("rex", ["send", "-s", "session-id", "-b", "omp-id", "--", self.prompt])
        self.split.assert_not_called()
        self.wait.assert_not_called()

    def test_running_omp_prefers_matching_cwd_and_falls_back_to_first(self) -> None:
        self.inspect.return_value = rex_session(PEER, {**PEER, "block_id": "second-id"})
        self.size.return_value = {}
        self.split.return_value = {"block_ids": ["new-pane"]}
        unrelated = {"foreground": {"name": "omp", "cwd": "/projects/my-app-other"}}
        cases = (
            (f"{self.cwd}/src", unrelated, self.cwd, "second-id"),
            (self.cwd, unrelated, f"{self.cwd}/src", "second-id"),
            (self.cwd, OMP, self.cwd, "second-id"),
            (self.cwd, unrelated, "/projects/other-app", "peer-id"),
            (self.cwd, OMP, "/projects/other-app", "peer-id"),
        )
        for cwd, first, second_cwd, expected in cases:
            with self.subTest(cwd=cwd, first_cwd=first["foreground"].get("cwd"), second_cwd=second_cwd):
                self.process.side_effect = [first, {"foreground": {"name": "omp", "cwd": second_cwd}}]
                self.send.reset_mock()
                result = dispatch.dispatch_handoff(cwd, self.prompt)
                self.assertEqual((result["method"], result["block_id"]), ("reuse-omp", expected))
                self.send.assert_called_once_with(
                    "rex", ["send", "-s", "session-id", "-b", expected, "--", self.prompt],
                )
        self.split.assert_not_called()
        self.wait.assert_not_called()

    def test_idle_shell_launch_quotes_cwd_and_explicit_auto_enter_submits(self) -> None:
        self.inspect.return_value = rex_session(PEER)
        self.process.return_value = IDLE
        cwd = "/projects/my app"
        result = dispatch.dispatch_handoff(cwd, self.prompt, auto_enter=True)
        self.assertEqual(result["method"], "reuse-shell")
        target = ["-s", "session-id", "-b", "peer-id"]
        self.assertEqual(self.send.call_args_list, [
            call("rex", ["send", *target, "--", "cd '/projects/my app' && omp"]),
            call("rex", ["send-key", *target, "enter"]),
            call("rex", ["send", *target, "--", self.prompt]),
            call("rex", ["send-key", *target, "enter"]),
        ])
        self.wait.assert_called_once_with("rex", "session-id", "peer-id")
        self.split.assert_not_called()

    def test_split_uses_adaptive_direction_and_starts_omp_in_interactive_shell(self) -> None:
        self.inspect.return_value = rex_session()
        self.size.return_value = {"columns": 155, "rows": 40, "cell_width_px": 9, "cell_height_px": 17}
        self.split.return_value = {"block_ids": ["new-pane", "source-id"]}
        result = dispatch.dispatch_handoff(self.cwd, self.prompt)
        self.assertEqual((result["method"], result["block_id"], result["split_direction"]), ("split", "new-pane", "right"))
        self.size.assert_called_once_with("rex", "session-id", "source-id")
        self.split.assert_called_once_with("rex", [
            "split", "-s", "session-id", "-b", "source-id", "--split=right", "-c", self.cwd,
            "--label", "omp", "--focus=false",
        ])
        self.sleep.assert_called_once_with(0.5)
        self.wait.assert_called_once_with("rex", "session-id", "new-pane")
        target = ["-s", "session-id", "-b", "new-pane"]
        self.assertEqual(self.send.call_args_list, [
            call("rex", ["send", *target, "--", "omp"]),
            call("rex", ["send-key", *target, "enter"]),
            call("rex", ["send", *target, "--", self.prompt]),
        ])

    def test_split_uses_explicit_direction(self) -> None:
        self.inspect.return_value = rex_session()
        self.size.return_value = {"cols": 155, "rows": 40, "cell_width_px": 9, "cell_height_px": 17}
        self.split.return_value = {"block_ids": ["new-pane"]}
        for direction in ("below", "above"):
            with self.subTest(direction=direction):
                self.split.reset_mock()
                result = dispatch.dispatch_handoff(self.cwd, self.prompt, direction=direction)
                self.assertEqual(result["split_direction"], direction)
                self.split.assert_called_once_with("rex", [
                    "split", "-s", "session-id", "-b", "source-id", f"--split={direction}", "-c", self.cwd,
                    "--label", "omp", "--focus=false",
                ])
        self.size.assert_not_called()

    def test_invalid_split_direction_is_rejected_before_contacting_rex(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid split direction"):
            dispatch.dispatch_handoff(self.cwd, self.prompt, direction="up")
        self.inspect.assert_not_called()

    def test_dispatch_requires_rex_block(self) -> None:
        with patch.dict(os.environ, {"REX_SESSION": "session-id"}, clear=True), self.assertRaisesRegex(
            dispatch.DispatchError, "REX_BLOCK is required",
        ):
            dispatch.dispatch_handoff(self.cwd, self.prompt)
        self.inspect.assert_not_called()

    def test_dispatch_rejects_unresolved_and_mismatched_sessions(self) -> None:
        unresolved = "Rex could not resolve the session containing pane source-id"
        cases = (
            (REX_BLOCK_ENV, None, unresolved),
            (REX_BLOCK_ENV, {**rex_session(PEER), "session_id": ""}, unresolved),
            (
                {**REX_ENV, "REX_SESSION": "old-session"},
                rex_session(PEER),
                "REX_SESSION mismatch for Rex pane source-id.*expected old-session, found session-id",
            ),
        )
        for env, info, message in cases:
            with self.subTest(env=env, info=info), patch.dict(os.environ, env, clear=True):
                self.inspect.return_value = info
                with self.assertRaisesRegex(dispatch.DispatchError, message):
                    dispatch.dispatch_handoff(self.cwd, self.prompt)
                self.process.assert_not_called()
                self.split.assert_not_called()
                self.send.assert_not_called()

    def test_cli_direction_reaches_rex_split_without_rex_session(self) -> None:
        self.inspect.side_effect = rex.inspect_session_for_block
        self.split.return_value = {"block_ids": ["new-pane"]}
        with patch.dict(os.environ, REX_BLOCK_ENV, clear=True), patch.object(
            rex, "run_rex_json", return_value=rex_session(),
        ), tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()) as out:
            cwd = Path(temporary).resolve()
            handoff = cwd / "handoff.md"
            handoff.touch()
            exit_code = dispatch.main([
                "--cwd", str(cwd), "--handoff", str(handoff), "--result", str(cwd / "result.md"),
                "--direction", "left",
            ])
        self.assertEqual(exit_code, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["session_id"], "session-id")
        self.assertEqual(report["split_direction"], "left")
        self.split.assert_called_once_with("rex", [
            "split", "-s", "session-id", "-b", "source-id", "--split=left", "-c", str(cwd),
            "--label", "omp", "--focus=false",
        ])

    def test_failed_omp_launch_does_not_send_prompt_to_shell(self) -> None:
        self.inspect.return_value = rex_session(PEER)
        self.process.return_value = IDLE
        self.wait.side_effect = dispatch.DispatchError("omp did not start")
        with self.assertRaisesRegex(dispatch.DispatchError, "omp did not start"):
            dispatch.dispatch_handoff(self.cwd, self.prompt, auto_enter=True)
        self.assertEqual(self.send.call_count, 2)


class DispatchCommandTests(unittest.TestCase):
    def test_doctor_falls_back_to_interactive_shell_and_report_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            settings = home / ".gemini/antigravity-cli/settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text('{"model": "Orchestrator (Example)"}', encoding="utf-8")
            env = {**REX_ENV, "SHELL": "/bin/zsh", "PI_CODING_AGENT_DIR": str(home / "custom-agent")}
            with patch.dict(os.environ, env, clear=True), patch.object(
                dispatch, "is_macos", return_value=True,
            ), patch.object(dispatch, "find_rex_bin", return_value="/tools/rex"), patch.object(
                dispatch, "run_rex_json", return_value={"session_id": "session-id", "block_id": "source-id"},
            ), patch.object(
                dispatch.shutil, "which", return_value=None,
            ) as which, patch.object(
                dispatch.subprocess, "run", side_effect=[
                    subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0, "true\n"),
                ],
            ) as run, patch.object(Path, "home", return_value=home):
                report = dispatch.doctor()
            self.assertTrue(report["ok"])
            self.assertEqual(report["checks"]["rex_session"], "session-id")
            self.assertEqual(report["checks"]["omp_config_path"], str(home / "custom-agent/config.yml"))
            self.assertEqual(report["checks"]["agy_selected_model"], "Orchestrator (Example)")
            self.assertEqual(len(report["warnings"]), 2)
            which.assert_called_once_with("omp")
            self.assertEqual(run.call_args_list[0].args[0], ["/bin/zsh", "-ic", "command -v omp"])
            self.assertEqual(run.call_args_list[1].args[0], ["defaults", "read", "com.superlogical.rex", "rex.agents.names-sessions"])

    def test_doctor_missing_core_check_is_false_but_reports_as_json(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch.object(
            dispatch, "is_macos", return_value=False,
        ), patch.object(dispatch, "find_rex_bin", return_value=None), patch.object(
            dispatch, "has_omp", return_value=False,
        ), patch.object(dispatch, "selected_agy_model", return_value=None), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(dispatch.main(["--doctor"]), 0)
        report = json.loads(out.getvalue())
        self.assertFalse(report["ok"])
        self.assertFalse(report["checks"]["inside_rex_session"])
        self.assertIsNone(report["checks"]["rex_session"])

    def test_doctor_checks_that_rex_environment_points_to_a_live_pane(self) -> None:
        live = {"session_id": "session-id", "block_id": "source-id"}
        warning = "Start agy in a live Rex pane with a matching REX_BLOCK value."
        cases = (
            (REX_BLOCK_ENV, live, True, "session-id"),
            ({**REX_BLOCK_ENV, "REX_SESSION": ""}, live, True, "session-id"),
            (REX_BLOCK_ENV, {"session_id": "session-id"}, False, "session-id"),
            (REX_BLOCK_ENV, {**live, "block_id": "other-pane"}, False, "session-id"),
            (REX_BLOCK_ENV, {"block_id": "source-id"}, False, None),
            (REX_BLOCK_ENV, OSError("Rex unavailable"), False, None),
            ({**REX_ENV, "REX_SESSION": "old-session"}, live, False, "old-session"),
        )
        with patch.object(dispatch, "is_macos", return_value=True), patch.object(
            dispatch, "find_rex_bin", return_value="rex",
        ), patch.object(dispatch, "has_omp", return_value=True), patch.object(
            dispatch, "rex_agent_naming_enabled", return_value=False,
        ), patch.object(dispatch, "selected_agy_model", return_value=None):
            for env, identity, inside, session_id in cases:
                with self.subTest(env=env, identity=identity), patch.dict(os.environ, env, clear=True), patch.object(
                    dispatch, "run_rex_json", side_effect=[identity],
                ) as whoami, redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(dispatch.main(["--doctor"]), 0)
                    report = json.loads(out.getvalue())
                    self.assertEqual(report["ok"], inside)
                    self.assertEqual(report["checks"]["inside_rex_session"], inside)
                    self.assertEqual(report["checks"]["rex_session"], session_id)
                    self.assertEqual(report["warnings"], [] if inside else [warning])
                    whoami.assert_called_once_with("rex", ["--autostart=false", "whoami"], timeout=2.0)

    def test_dry_run_builds_prompt_without_reading_handoff_or_contacting_rex(self) -> None:
        with patch.object(dispatch, "dispatch_handoff") as live, patch.object(
            dispatch, "find_rex_bin",
        ) as find, redirect_stdout(io.StringIO()) as out:
            exit_code = dispatch.main([
                "--handoff", "/tmp/missing-handoff.md", "--result", "/tmp/result.md", "--cwd", "/projects/my-app",
                "--scale", "orchestrate", "--lang", "en", "--direction", "right", "--dry-run",
            ])
        self.assertEqual(exit_code, 0)
        report = json.loads(out.getvalue())
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["direction"], "right")
        self.assertTrue(report["prompt"].startswith("orchestrate Read /tmp/missing-handoff.md"))
        live.assert_not_called()
        find.assert_not_called()

    @unittest.skipIf(sys.version_info < (3, 11), "Python -P requires Python 3.11 or later")
    def test_dry_run_imports_local_module_with_safe_path(self) -> None:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("REX_HANDOFF_LANG", None)
        completed = subprocess.run([
            sys.executable, "-P", str(SCRIPTS / "dispatch_handoff.py"),
            "--handoff", "/tmp/a.md", "--result", "/tmp/b.md", "--dry-run",
        ], env=env, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["direction"], "auto")


if __name__ == "__main__":
    unittest.main()

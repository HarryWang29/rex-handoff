---
name: rex-handoff
description: Use to plan a handoff and dispatch to omp, review omp changes, or run the environment doctor in Rex. 用于规划交接、派发给 omp、初审 omp 改动或诊断 Rex 环境。
---

# Rex Handoff

## Core Rules

- 在 `agy` 用当前单模型澄清需求、探索代码、制定方案、写交接和初审结果。通过 `omp` 实施。此流程禁止调用 `invoke_subagent`、`define_subagent`、`run_workflow` 或 `swarm`。
- 方案、交接和初审之前，读取仓库内存在且适用的 `AGENTS.md`、`CLAUDE.md`、`.omp/AGENTS.md`、`.omp/RULES.md`、`.claude/rules/`，以及目标目录各级 `AGENTS.md`。对照同类模块，沿用仓库的分层、命名、边界校验与检查命令。
- 把 Handoff 和 Result 临时文档写入 `/tmp/`。提交、推送、创建 PR 和改写 Git 历史需要用户明确授权。
- 相对于这份 `SKILL.md` 解析 `./scripts/dispatch_handoff.py` 和 `./scripts/rex_auto_title.py`。命令的 `Cwd` 可设为 Skill 目录，脚本的 `--cwd` 传入实际项目工作区。调度脚本的每次调用（含 `--doctor`）都在 `run_command` 中设置 `BypassSandbox: true`。默认只预填 `omp` 提示，等用户按回车。

## Phase 1 — Dispatch Task to omp

1. **确认基线。** 记录 `git status --short --branch` 和项目实际工作区。以 `origin/main` 为方案与差异的基线，确认该引用和当前提交的关系，标记用户已有改动。分支、worktree 或其他准备步骤若需执行，写清目标和顺序，交给 `omp` 主控在派发前完成。若 `origin/main` 缺失，在交接中写明并记录可用的基线和原因。若仓库没有初始提交，记为无提交并选 `simple`，在现有工作区实施。
2. **确定规模。** `simple` 适用于一个有明确范围的修改单元。由 `omp` 用 `task` 委派一位子 Agent 完成代码和单测，主控统一验证。`orchestrate` 适用于多个能独立交付的切片。写清各切片的文件边界、共享接口和依赖顺序，要求 `omp` 用 `task` 并行执行独立部分，再统一验证。选 `simple` 时，Handoff 必须明确单个 `task` 委派，不得要求“单线直接实施”或 `implement inline`。
3. **写六段交接。** 用同一时间戳生成 `/tmp/handoff-<topic>-<timestamp>.md` 和 `/tmp/handoff-result-<topic>-<timestamp>.md`，用用户的语言书写。交接里的每项要求都应对应一个可检查的结果。

   1. **目标与背景** — 用户意图、验收行为和适用背景。
   2. **前置动作** — 仓库路径、当前分支与基线、已有改动、派发前必须完成的准备。
   3. **规模与编排** — `simple` 或 `orchestrate`，主控与子 Agent 职责、切片边界、共享接口和依赖。
   4. **文件与修改规格** — 目标文件或模块、要求的行为、边界情形和预期测试。
   5. **约束与授权** — 适用的仓库规范、用户授权、禁止触碰的文件或行为。
   6. **验证与结果回写** — 主控运行的准确命令、预期结果、Result 的绝对路径，以及文件清单、检查结果与限制的报告要求。

4. **发到 omp。** 在 Skill 目录运行下列命令，将路径替换为第 3 步的绝对路径，将 `--scale` 设为第 2 步的结果，中文用 `--lang zh`，英文用 `--lang en`。保持默认的 `task` Profile 选择，用户明确指定 `omp` 模型标签时可传 `--model-tag`。用户明确指定分屏方向时可传 `--direction below|right|above|left`（默认 `auto`）。

   ```sh
   python3 ./scripts/dispatch_handoff.py --cwd /path/to/my-app \
     --handoff /tmp/handoff-user-auth-20260101-120000.md \
     --result /tmp/handoff-result-user-auth-20260101-120000.md \
     --scale simple --lang zh
   ```

5. **汇报交接状态。** 用用户语言说明规模与确认过的基线、Handoff 和 Result 路径、脚本报告的目标面板，并提示用户在 `omp` 核对已预填的提示后按回车。派发成功以脚本返回目标面板为准。

## Phase 2 — Review omp Output

1. 读取本次 `/tmp/handoff-result-*.md`，查看实际工作区的 `git status --short --branch`、`git diff` 和 `git diff --cached`，并打开相关未跟踪文件。若 `omp` 使用独立分支，也按交接基线检查该分支的提交差异。结果缺失、报错或与 Git 状态冲突时，再用 `rex capture -s <session> -b <block> --trim` 查看目标面板。
2. 逐项对照 Handoff、仓库规则和实际改动。核对验收行为与边界情况，区分已有改动与本轮实施。把验证结果归因到实际执行者，指出交接内覆盖不足、回归、越界改动或未通过的检查。
3. 用用户语言汇报发现、相关文件和行号、验证情况与剩余决策，并提示用户在 VS Code 里终审实际 Diff。

## Setup & Diagnostics

- 在 Skill 目录运行 `python3 ./scripts/dispatch_handoff.py --doctor`，为该 `run_command` 设置 `BypassSandbox: true`。根据 `checks` 与 `warnings` 汇报环境状态。使用 Rex 启动 `agy`，在 `/model` 选择一个模型，关闭 Rex 的 Agent Session Naming，以便目录会话标题由本插件同步。
- 在 Skill 目录运行 `python3 ./scripts/rex_auto_title.py print-zsh-hook` 可生成可选的 Zsh 片段。用户可将输出粘贴进自己的 `~/.zshrc`，让切换目录时也同步标题。命令只打印内容。

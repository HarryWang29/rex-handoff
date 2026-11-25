# Rex Handoff collaboration rules

## Single-model role in agy

Use the current single model in `agy` to clarify requirements, explore the code, agree on a plan, write the handoff, and conduct the initial review. Route implementation to `omp` through `/rex-handoff`. Keep `invoke_subagent`, `define_subagent`, `run_workflow`, and `swarm` out of this workflow.

## Repository constraints first

Before settling a plan, writing a handoff, or reviewing changes, read the applicable `AGENTS.md`, `CLAUDE.md`, `.omp/AGENTS.md`, `.omp/RULES.md`, and `.claude/rules/` files wherever present. Read `AGENTS.md` in the target service directories as well. Check comparable modules and follow their layering, naming, boundary validation, and verification commands.

## Workspace and Git hygiene

Write temporary handoff and result documents under `/tmp/`. Obtain explicit user authorization before committing, pushing, opening a pull request, or rewriting Git history.

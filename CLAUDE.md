# GoldScout — Claude Code Entry Point

Before doing any work in this repository:

1. Read `AGENTS.md`.
2. Read `CURRENT_TASK.md`.
3. Read only the sections of `CONTEXT.md`, `ARCHITECTURE.md`, and `ROADMAP.md` needed for the current task.
4. Treat repository code and tests as the source of truth when documentation conflicts.
5. Work on a dedicated task branch; do not edit `main` directly.
6. Do not expand scope without explicit approval.
7. Preserve all safety constraints in `AGENTS.md`.

## Current collaboration model

Claude = primary implementer.
Codex = primary reviewer/validator.
Human owner = merge/strategy approval.

Do not let Claude and Codex modify the same files concurrently unless the owner explicitly coordinates it.

## Required task completion report

Return:
- objective / cause
- files changed
- tests run
- MQL compile result
- behavior verification
- limitations
- branch + commit hash
- PR link if created

Do not automatically merge your own implementation.

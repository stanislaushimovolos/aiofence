---
allowed-tools: Bash(git:*), Bash(uv run ruff:*)
description: Create a conventional commit from staged changes
---

Create a commit following the project's conventional commit style.

## Flow

1. **Check for changes** — run `git status` and `git diff` to see all changes
2. **Run formatting** — run `uv run ruff format .` and `uv run ruff check --fix .` to auto-fix style issues
3. **Stage tracked files** — run `git add -u` to stage modified/deleted tracked files only (new files must be staged manually before running this command)
4. **Analyze changes** — understand what was changed and why
5. **Generate commit message** — create message following conventional commit format
6. **Ask for confirmation** — use **AskUserQuestion** tool with "Approve" / "Edit message" / "Cancel" options, showing the proposed commit message
7. **Commit** — if approved, create the commit (do NOT push)

## Conventional Commit Format

Pattern: `<type>: <description>`

Types:
- `feat` — new feature
- `fix` — bug fix
- `refactor` — code restructuring without behavior change
- `test` — adding/updating tests
- `docs` — documentation changes
- `chore` — maintenance, dependencies, configs

Examples:
- `feat: add deadline propagation via headers`
- `fix: handle zero timeout in TimeoutSource`
- `refactor: extract source lifecycle to SourceSet`
- `test: add nested scope cancellation tests`

## Guidelines

- Focus on the "why" rather than the "what"
- Keep description concise (1-2 sentences)
- If no changes exist, tell the user there's nothing to commit
- Do NOT push after committing
- Do NOT add Co-Authored-By or other AI markers

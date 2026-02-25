---
allowed-tools: Bash(git:*)
description: Create a new branch from main with correct naming
---

Create a new branch following the project's branch naming convention.

## Flow

1. **Check for uncommitted changes** — run `git status`
   - If there are uncommitted changes (staged or unstaged), **abort** with message:
     "Cannot create branch: you have uncommitted changes. Please commit or stash them first."

2. **Update main** — run these commands sequentially:
   - `git fetch origin`
   - `git checkout main`
   - `git pull origin main`

3. **Ask for task description** — use **AskUserQuestion** tool
   - Question: "Describe the task you're working on"
   - Let user describe in natural language

4. **Process description semantically**:
   - **Infer branch type** from description:
     - `feat` — new feature ("add", "implement", "create", "new")
     - `fix` — bug fix ("fix", "bug", "broken", "error", "issue")
     - `refactor` — restructuring ("refactor", "restructure", "reorganize", "clean up")
     - `chore` — maintenance ("update", "bump", "upgrade", "dependencies", "config")
     - `test` — testing ("test", "coverage", "spec")
     - `docs` — documentation ("docs", "documentation", "readme")
   - **Generate short description**: extract key concepts, convert to lowercase with hyphens
   - If type is ambiguous, ask clarifying question with options

5. **Show for review** — use **AskUserQuestion** tool
   - Show the generated branch name: `<type>/<short-description>`
   - Options: "Create branch" / "Edit" / "Cancel"

6. **Create branch** — if approved:
   - Run `git checkout -b <branch-name>`
   - Confirm: "Branch created and checked out: `<branch-name>`"

## Branch Naming Convention

Pattern: `<type>/<short-description>`

Examples:
- `feat/deadline-propagation`
- `fix/zero-timeout-handling`
- `refactor/extract-source-set`
- `test/nested-scope-cancellation`

## Guidelines

- Keep generated description concise (2-4 words)
- Do NOT push the branch — only create locally
- If any git command fails, show error and stop

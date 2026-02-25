# Coding Principles

## Code Style

- Type hints everywhere
- Short focused functions; high-level first, helpers later
- f-strings, double quotes
m- Relative imports inside the package, absolute otherwise. Only one-level relative imports (`from .core`, not `from ..core`)
- Newspaper-style: public API at the top, private helpers below
- Composition over inheritance

## Comments

- Code should be self-explanatory; no comments by default
- Only add docstrings for public API methods
- When docstrings are needed, use multi-line format:
  ```python
  """
  Description here.
  """
  ```
- Inline comments (`#`) only for the most complex logic

## Testing

- One behavior per test
- AAA pattern (Arrange -> Act -> Assert)
- Naming: `test__Subject__Condition__ExpectedResult`
- `conftest.py` only for shared fixtures; otherwise keep fixtures in local test files

## Git

### Conventional Commits

Pattern: `<type>: <description>`

Types:
- `feat` — new feature
- `fix` — bug fix
- `refactor` — code restructuring without behavior change
- `test` — adding/updating tests
- `docs` — documentation changes
- `chore` — maintenance, dependencies, configs

Examples:
- `feat: add streaming support for deadline propagation`
- `fix: handle zero timeout in TimeoutSource`
- `refactor: extract source lifecycle to SourceSet`
- `test: add nested scope cancellation tests`

### Before Committing

Run formatting before every commit.

---

## For AI Assistants

### Communication

- Be concise; avoid over-explaining
- Use English

### Workflow

- Brief plan before starting
- TDD when defining new behavior: interface -> tests -> implementation
- If docs and code are inconsistent — highlight and ask, or fix immediately

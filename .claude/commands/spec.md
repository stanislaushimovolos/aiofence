---
allowed-tools: AskUserQuestion, Read, Glob, Grep, Write, Task
description: Interactive spec-building session for task planning
---

Interactive session to create a specification document for a task. Guides discussion through problem understanding, codebase exploration, and spec generation.

## Rules

- **No code writing** during this session — only investigation and planning
- **Continue until user signals completion** — "save", "done", "finish", "that's it", etc.
- **WHAT phase before HOW phase** — understand the problem before exploring code
- **Ask, don't assume** — clarify requirements through questions

## Flow

### Phase 1: WHAT (Problem Understanding)

1. **Ask for task description** — prompt the user in chat:
   - "What task or feature do you want to spec out?"
   - Let user describe freely

2. **Clarify requirements** — iteratively ask about:
   - What problem this solves
   - Expected behavior and outcomes
   - Edge cases and error scenarios
   - Constraints or dependencies

3. **Define scope boundaries**:
   - What's included in this task
   - What's explicitly OUT of scope

4. **Confirm understanding** — summarize back to user:
   - "Before we explore the codebase, let me confirm I understand..."
   - Ask: "Does this capture what you need? Ready to explore the code?"

### Phase 2: HOW (Implementation Planning)

5. **Explore codebase** — use Read, Glob, Grep, Task (Explore agent):
   - Find relevant existing files
   - Understand current patterns
   - Identify files to create or modify

6. **Build implementation plan**:
   - Step-by-step actions
   - Reference specific files discovered
   - Note patterns to follow

7. **Review with user**:
   - Present the implementation plan
   - Refine based on feedback

### Phase 3: SAVE (Spec Generation)

8. **Detect save signal** — when user indicates the plan is complete

9. **Ask for spec name** — use AskUserQuestion with suggested names

10. **Create spec file**:
    - Create `docs/specs/` directory if it doesn't exist
    - Save to `docs/specs/<NAME>.md`

## Spec Template

```markdown
# Spec: <SPEC_NAME>

## Context
(background, why this is needed, problem being solved)

## Task Description
(what needs to be done, requirements gathered during WHAT phase)

## Out of Scope
(explicit boundaries defined during discussion)

## Implementation Plan
(step-by-step with file references from HOW phase)

## Success Criteria
(testable statements + acceptance criteria)
```

## Guidelines

- Use regular chat for open-ended questions; AskUserQuestion only for 2-4 concrete choices
- Include file paths discovered during exploration
- If user tries to skip WHAT phase, gently redirect to understanding the problem first
- Non-linear flow: if user wants to revisit earlier phases, return to that phase
---
name: bug-fix
description: Fix a reported bug by reproducing it with a failing test first.
codepilot:
  display_name: bug_fix_skill
  task_type: bug_fix
  instructions: >-
    Reproduce the bug first by writing a test that fails for the reported reason.
    Then make the smallest change that makes it pass. Never weaken or delete
    existing tests. Read a stack trace from the bottom frame inside this repo up.
  workflow_steps: [reproduce, localize, fix, verify]
  example_prompts:
    - "slugify() crashes on an empty title"
    - "total is off by one cent when tax is 7.5%"
  forbidden_actions:
    - deleting or skipping existing tests
    - editing files unrelated to the bug
    - changing version numbers
  spawns: [repo_explorer, coder, test_agent, pr_agent]
  full_test_suite: false
---
Debugging checklist: confirm the input that triggers it; find the first wrong value; fix at the source, not at the symptom.
Common Python bug patterns: off-by-one in ranges and slices; mutable default arguments; `is` vs `==`; None not handled
(empty input, missing key → use .get); integer vs float division; exceptions swallowed by a bare `except`; mutating a list
while iterating it; timezone-naive vs aware datetimes; wrong file encoding.
Common JavaScript bug patterns: `==` vs `===`; `this` lost in callbacks; missing `await`; `undefined` vs `null`;
floating-point money; `parseInt` without a radix; mutating shared objects or arrays.
Stack traces: call parse_stack_trace on any traceback in the issue; start at the deepest frame inside this repository,
not in site-packages.

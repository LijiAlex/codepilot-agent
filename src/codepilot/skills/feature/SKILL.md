---
name: feature
description: Add a feature by following existing patterns in the repository.
codepilot:
  display_name: feature_addition_skill
  task_type: feature_addition
  instructions: >-
    Read one or two similar existing features first and follow their structure.
    Keep public interfaces backward compatible. Add tests for the new behaviour
    and update documentation the feature touches.
  workflow_steps: [explore_pattern, design, implement, test, document]
  example_prompts: ["add a --json flag to the CLI"]
  forbidden_actions: ["breaking existing public functions", "adding dependencies without a manifest change"]
  spawns: [repo_explorer, coder, test_agent, pr_agent]
  full_test_suite: false
---
Interface design checklist: name it like its neighbours; keyword arguments with safe defaults for anything new; validate
inputs at the boundary; return types consistent with similar functions; errors raised the same way the module already does.
Backward compatibility: existing callers and tests keep working unchanged; never rename or remove a public function or
argument (add a new one, keep the old as an alias if needed).
Documentation requirement: update the docstring, the README section or usage example the feature touches, and the changelog
if the repository keeps one.

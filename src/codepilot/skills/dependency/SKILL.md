---
name: dependency
description: Update a dependency, check its changelog and run the full test suite.
codepilot:
  display_name: dependency_update_skill
  task_type: dependency_update
  instructions: >-
    Use fetch_changelog to read what changed between the versions. Change only
    the dependency manifest (and the lockfile if the repo has one). Never run
    pip or uv install yourself: code reinstalls from the manifest. Run the full
    test suite with run_tests.
  workflow_steps: [check_changelog, update, resolve_conflicts, test_all]
  example_prompts: ["bump pyyaml to 6.0.3"]
  forbidden_actions: ["installing packages by shell", "editing code to silence deprecation warnings unless asked"]
  spawns: [repo_explorer, coder, test_agent, pr_agent]
  full_test_suite: true
---
Common breaking-change patterns: renamed keyword arguments, removed aliases, changed defaults.
Breaking-change patterns per ecosystem:
- pip (Python): renamed or removed keyword arguments; changed defaults; dropped Python versions (`requires-python`);
  moved modules (import paths); stricter type checks in pydantic-style libraries; pinned upper bounds in other packages.
- npm (JavaScript): major-version ESM/CommonJS switches; removed default exports; peer-dependency ranges; Node engine bumps.
- cargo (Rust): feature flags renamed or no longer default; MSRV (minimum Rust version) bumps; trait/method renames.
Lockfiles: if the repository has one (uv.lock, poetry.lock, package-lock.json, Cargo.lock), say in the PR that it must be
regenerated; you cannot run the package manager.

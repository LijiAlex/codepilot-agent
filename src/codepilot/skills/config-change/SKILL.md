---
name: config-change
description: Change a setting consistently everywhere it is defined.
codepilot:
  display_name: config_change_skill
  task_type: config_change
  instructions: >-
    Find every place the setting is defined before changing any of them. Change
    them consistently. Confirm each file still parses, then run the full test
    suite. Never touch CI workflows, secrets or deployment files.
  workflow_steps: [locate_all, change_consistently, validate_parse, test_all]
  example_prompts: ["pytest.ini should only collect from tests/"]
  forbidden_actions: ["editing .github/", "editing secrets or credentials files"]
  spawns: [repo_explorer, coder, test_agent, pr_agent]
  full_test_suite: true
---

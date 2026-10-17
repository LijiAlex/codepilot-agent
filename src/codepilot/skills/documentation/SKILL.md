---
name: documentation
description: Update documentation in the repository's existing style.
codepilot:
  display_name: documentation_skill
  task_type: documentation
  instructions: >-
    Match the existing documentation style and headings. Include a short code
    example when documenting an API. Update the README index if a public API changed.
  workflow_steps: [read_existing, draft, review_accuracy, update_index]
  example_prompts: ["README does not mention the memory API"]
  forbidden_actions: ["changing code", "inventing behaviour the code does not have"]
  spawns: [repo_explorer, coder, pr_agent]
  full_test_suite: false
---

# tests/test_skills.py
from codepilot.skills import load_skills, render_skill_prompt, TASK_TYPES

def test_all_five_skills_load_with_brief_fields():
    skills = load_skills()
    assert set(skills) == set(TASK_TYPES)
    bug = skills["bug_fix"]
    assert bug.display_name == "bug_fix_skill"
    assert bug.workflow_steps[:2] == ["reproduce", "localize"]
    assert bug.forbidden_actions and bug.example_prompts and bug.instructions
    assert "test_agent" in bug.spawns
    assert "test_agent" not in skills["documentation"].spawns
    assert skills["dependency_update"].full_test_suite is True

def test_render_includes_forbidden_actions_and_steps():
    text = render_skill_prompt(load_skills()["bug_fix"])
    assert "Forbidden" in text and "reproduce" in text


def test_skills_carry_the_brief_contents():
    from codepilot.skills import load_skills
    sk = load_skills()
    assert "JavaScript bug patterns" in sk["bug_fix"].notes and "parse_stack_trace" in sk["bug_fix"].notes
    assert "Interface design checklist" in sk["feature_addition"].notes and "Backward compatibility" in sk["feature_addition"].notes
    assert all(e in sk["dependency_update"].notes for e in ("pip (Python)", "npm (JavaScript)", "cargo (Rust)"))

# playground/plants.py
"""Changes applied to the copy of guardrail-eval-pipeline v0.2.0 to create real, documented defects."""
PLANTS = [
    # citation-off-by-one: the last passage (【N】 with N passages) is reported out of range.
    ("src/guardrail_eval_pipeline/guardrails/deterministic.py",
     "if index < 1 or index > len(contexts):", "if index < 1 or index >= len(contexts):"),
    # memory-twin: a second bug in the same file as citation-off-by-one, filed only after that one is merged, so its
    # lesson (file + approach) is recalled — the memory before/after demo (Task 27 Step 3b).
    ("src/guardrail_eval_pipeline/guardrails/deterministic.py",
     "    return tuple(sorted(set(found)))", "    return tuple(found)"),
    # attack-env / attack-push need these to exist so the request is concrete.
    ("README.md", "## Quick start\n", "## Quick start\n\nYou will recieve a guardrail id after the first apply.\n"),
    # contract-lowercase (retry gate): a contract test the requested change must break. It is protected in codepilot.yml
    # (`tests/test_contract_*.py`), so agents cannot edit it; verification fails twice → "retry after 2 failed test runs".
    ("tests/test_contract_normalise.py", None,
     '"""Contract with downstream consumers: normalise() strips citation markers and keeps the original case."""\n'
     "from guardrail_eval_pipeline.guardrails.bedrock import normalise\n\n\n"
     "def test_normalise_keeps_case():\n"
     '    assert normalise("Ward B uses PROC-RAD-01 【1†L1-L3】") == "Ward B uses PROC-RAD-01 "\n'),
]

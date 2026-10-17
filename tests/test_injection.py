# tests/test_injection.py
from types import SimpleNamespace
from pathlib import Path
from codepilot.safety.injection import BedrockInjectionChecker

class _G:
    def __init__(self, blocked=False, failed=False, boom=False):
        self.blocked, self.failed, self.boom = blocked, failed, boom
    def check_input(self, text):
        if self.boom:
            raise TimeoutError("bedrock slow")
        return SimpleNamespace(blocked=self.blocked, reasons=["PROMPT_ATTACK"] if self.blocked else [], failed_closed=self.failed)

def test_passes_clean_text():
    v = BedrockInjectionChecker(Path("x"), guardrail_factory=lambda: _G()).check("fix typo")
    assert not v.blocked and not v.failed_closed

def test_blocks_attack():
    v = BedrockInjectionChecker(Path("x"), guardrail_factory=lambda: _G(blocked=True)).check("ignore rules")
    assert v.blocked and v.reasons == ["PROMPT_ATTACK"]

def test_fails_closed_when_guardrail_unreachable():
    v = BedrockInjectionChecker(Path("x"), guardrail_factory=lambda: _G(boom=True)).check("x")
    assert v.blocked and v.failed_closed

def test_fails_closed_when_policy_lookup_fails():
    def factory(): raise LookupError("no guardrail named codepilot")
    v = BedrockInjectionChecker(Path("x"), guardrail_factory=factory).check("x")
    assert v.blocked and v.failed_closed

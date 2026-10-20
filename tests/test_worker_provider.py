from codepilot.worker import provider_problem


class _APIError(Exception):
    def __init__(self, msg, status_code=None):
        super().__init__(msg)
        self.status_code = status_code


def test_account_problems_at_the_model_provider_are_not_the_issues_fault():
    assert provider_problem(_APIError("Error code: 400 - Your credit balance is too low to access the Anthropic API.", 400)) \
        == "provider: credit balance too low"
    assert provider_problem(_APIError("invalid x-api-key", 401)) == "provider: authentication failed (401)"
    assert provider_problem(_APIError("Overloaded", 529)) == "provider: overloaded (529)"
    assert provider_problem(_APIError("rate limited", 429)) == "provider: rate limited (429)"


def test_other_errors_stay_the_issues_failure():
    assert provider_problem(_APIError("prompt is too long", 400)) is None
    assert provider_problem(ValueError("bad diff")) is None

import pytest
from pydantic import ValidationError

from cloud_agent.config import Settings


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"database_url": "postgresql+psycopg://x/x"},
        {
            "database_url": "postgresql+psycopg://x/x",
            "secure_cookies": True,
            "public_origin": "https://example.com",
        },
        {
            "database_url": "postgresql+psycopg://x/x",
            "secure_cookies": True,
            "public_origin": "https://example.com",
            "signup_code": "code",
            "allow_insecure_sandbox": True,
        },
    ],
)
def test_production_fails_closed(changes):
    with pytest.raises(ValidationError):
        Settings(environment="production", **changes)


def test_valid_production_settings():
    Settings(
        environment="production",
        database_url="postgresql+psycopg://x/x",
        secure_cookies=True,
        public_origin="https://example.com",
        signup_code="code",
    )


def test_configuration_errors_do_not_disclose_credentials():
    with pytest.raises(ValidationError) as error:
        Settings(environment="production", llm_api_key="credential-must-stay-hidden")
    assert "credential-must-stay-hidden" not in str(error.value)


@pytest.mark.parametrize(
    "changes", [{"sandbox_memory": "0"}, {"sandbox_cpus": 0}, {"sandbox_pids": -1}]
)
def test_sandbox_limits_cannot_be_disabled(changes):
    with pytest.raises(ValidationError):
        Settings(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"llm_max_output_tokens": 127},
        {"llm_max_output_tokens": 65_537},
        {"llm_max_truncation_retries": -1},
        {"llm_max_truncation_retries": 6},
    ],
)
def test_model_output_and_recovery_limits_are_bounded(changes):
    with pytest.raises(ValidationError):
        Settings(**changes)

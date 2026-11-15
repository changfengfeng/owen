from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLOUD_", env_file=None, hide_input_in_errors=True)

    environment: str = "development"
    database_url: str = "sqlite:///./data/cloud-agent.db"
    public_origin: str = "http://localhost:8000"
    secure_cookies: bool = False
    trusted_proxies: str = "127.0.0.1"
    registration_enabled: bool = True
    signup_code: SecretStr = SecretStr("")
    session_hours: int = Field(default=168, gt=0)
    daily_tasks: int = 20
    concurrent_tasks: int = 2
    monthly_tokens: int = 500_000
    task_token_budget: int = 24_000
    task_timeout_seconds: int = 900
    lease_seconds: int = 60
    poll_seconds: float = Field(default=1.0, gt=0)
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_api_key: SecretStr = SecretStr("")
    llm_max_turns: int = Field(default=24, gt=0)
    llm_max_output_tokens: int = Field(default=4096, ge=128, le=65_536)
    llm_max_truncation_retries: int = Field(default=2, ge=0, le=5)
    sandbox_image: str = "cloud-agent-sandbox:local"
    sandbox_runtime: str = "runsc"
    allow_insecure_sandbox: bool = False
    sandbox_memory: str = Field(default="512m", pattern=r"^[1-9][0-9]*[kKmMgG]?$")
    sandbox_cpus: float = Field(default=1.0, gt=0)
    sandbox_pids: int = Field(default=128, gt=0)
    max_output_bytes: int = Field(default=32_768, gt=0)
    max_patch_bytes: int = Field(default=1_048_576, gt=0)

    @model_validator(mode="after")
    def validate_security(self):
        if self.environment not in {"development", "test", "production"}:
            raise ValueError("Unknown environment")
        if (
            min(
                self.daily_tasks, self.concurrent_tasks, self.monthly_tokens, self.task_token_budget
            )
            < 1
        ):
            raise ValueError("Quotas must be positive")
        if self.lease_seconds < 15 or self.task_timeout_seconds < 1:
            raise ValueError("Invalid task timing")
        if self.environment == "production":
            if not self.database_url.startswith("postgresql"):
                raise ValueError("Production requires PostgreSQL")
            if not self.secure_cookies or not self.public_origin.startswith("https://"):
                raise ValueError("Production requires HTTPS and secure cookies")
            if self.allow_insecure_sandbox or self.sandbox_runtime != "runsc":
                raise ValueError("Production requires gVisor (runsc); no insecure fallback")
            if self.registration_enabled and not self.signup_code.get_secret_value():
                raise ValueError(
                    "Production registration requires an operator-provided signup code"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

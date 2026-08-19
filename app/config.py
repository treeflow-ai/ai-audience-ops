from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


@dataclass(slots=True)
class Settings:
    """Runtime configuration loaded when each Settings instance is created.

    Using factories instead of import-time environment lookups keeps tests and
    CLI usage predictable when environment variables change between runs.
    """

    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///./var/audience_ops.db"))
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "mock"))
    openai_model: str = field(default_factory=lambda: _env("OPENAI_MODEL", "gpt-5.5"))
    approval_threshold: int = field(default_factory=lambda: _env_int("APPROVAL_THRESHOLD", 5000))
    synthetic_student_count: int = field(default_factory=lambda: _env_int("SYNTHETIC_STUDENT_COUNT", 12000))
    allow_real_marketing_sync: bool = field(default_factory=lambda: _bool("ALLOW_REAL_MARKETING_SYNC", False))
    real_sync_max_recipients: int = field(default_factory=lambda: _env_int("REAL_SYNC_MAX_RECIPIENTS", 500))
    policy_dir: Path = field(default_factory=lambda: Path(_env("POLICY_DIR", "policies")))
    mock_sync_log: Path = field(default_factory=lambda: Path(_env("MOCK_SYNC_LOG", "var/mock_marketing_syncs.jsonl")))

    learndash_base_url: str | None = field(default_factory=lambda: os.getenv("LEARNDASH_BASE_URL"))
    learndash_username: str | None = field(default_factory=lambda: os.getenv("LEARNDASH_USERNAME"))
    learndash_app_password: str | None = field(default_factory=lambda: os.getenv("LEARNDASH_APP_PASSWORD"))

    mailchimp_api_key: str | None = field(default_factory=lambda: os.getenv("MAILCHIMP_API_KEY"))
    mailchimp_server_prefix: str | None = field(default_factory=lambda: os.getenv("MAILCHIMP_SERVER_PREFIX"))
    mailchimp_list_id: str | None = field(default_factory=lambda: os.getenv("MAILCHIMP_LIST_ID"))

    constant_contact_access_token: str | None = field(default_factory=lambda: os.getenv("CONSTANT_CONTACT_ACCESS_TOKEN"))
    constant_contact_list_id: str | None = field(default_factory=lambda: os.getenv("CONSTANT_CONTACT_LIST_ID"))
    constant_contact_activity_timeout_seconds: int = field(
        default_factory=lambda: _env_int("CONSTANT_CONTACT_ACTIVITY_TIMEOUT_SECONDS", 60)
    )

    def __post_init__(self) -> None:
        self.llm_provider = self.llm_provider.strip().lower()
        if self.llm_provider not in {"mock", "openai"}:
            raise ValueError("LLM_PROVIDER must be either 'mock' or 'openai'.")
        if self.approval_threshold < 0:
            raise ValueError("APPROVAL_THRESHOLD must be zero or greater.")
        if self.synthetic_student_count <= 0:
            raise ValueError("SYNTHETIC_STUDENT_COUNT must be greater than zero.")
        if self.real_sync_max_recipients <= 0:
            raise ValueError("REAL_SYNC_MAX_RECIPIENTS must be greater than zero.")
        if self.constant_contact_activity_timeout_seconds <= 0:
            raise ValueError("CONSTANT_CONTACT_ACTIVITY_TIMEOUT_SECONDS must be greater than zero.")
        if self.llm_provider == "openai" and not self.openai_model.strip():
            raise ValueError("OPENAI_MODEL must be configured when LLM_PROVIDER=openai.")

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LCF_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    host: str = "0.0.0.0"
    port: int = 8090
    log_level: str = "INFO"
    debug: bool = False

    db_url: str

    llm_base_url: str = ""
    llm_api_key: str = "not-needed"
    llm_model: str = ""
    llm_max_images_per_call: int = 4
    # A ceiling, not a target. Without it a constrained array schema lets the
    # model emit rows until it exhausts the context — minutes of generation for
    # a table that should have four entries. Set generously: a JSON grammar also
    # permits unlimited whitespace, and truncating mid-object costs a full retry.
    llm_max_tokens: int = 4096
    llm_context_window: int = 32768
    llm_temperature: float = 0.2
    llm_timeout_s: int = 180
    llm_concurrency: int = 4

    s3_endpoint: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "us-east-1"
    s3_bucket_uploads: str = "lcf-uploads"
    s3_bucket_templates: str = "lcf-templates"
    s3_bucket_exports: str = "lcf-exports"

    worker_in_process: bool = True

    @property
    def buckets(self) -> list[str]:
        return [self.s3_bucket_uploads, self.s3_bucket_templates, self.s3_bucket_exports]


@lru_cache
def settings() -> Settings:
    return Settings()

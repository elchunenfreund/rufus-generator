import structlog
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App
    app_name: str = "Rufus Generator"
    environment: str = "development"
    debug: bool = False

    # Auth
    auth_username: str = "admin"
    auth_password: SecretStr = SecretStr("changeme")

    # Database
    database_url: str = "postgresql://localhost:5432/rufus_generator"

    # Redis / Celery
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    @model_validator(mode="after")
    def fix_heroku_urls(self):
        # Heroku uses postgres:// but SQLAlchemy requires postgresql://
        if self.database_url.startswith("postgres://"):
            self.database_url = self.database_url.replace("postgres://", "postgresql://", 1)
        # Heroku Redis uses rediss:// (SSL) with self-signed certs — add ssl_cert_reqs
        if self.redis_url.startswith("rediss://") and "ssl_cert_reqs" not in self.redis_url:
            sep = "&" if "?" in self.redis_url else "?"
            self.redis_url = f"{self.redis_url}{sep}ssl_cert_reqs=CERT_NONE"
        # Default celery URLs to redis_url if not explicitly set
        if self.celery_broker_url == "redis://localhost:6379/0" and self.redis_url != "redis://localhost:6379/0":
            self.celery_broker_url = self.redis_url
        if self.celery_result_backend == "redis://localhost:6379/1" and self.redis_url != "redis://localhost:6379/0":
            self.celery_result_backend = self.redis_url
        return self

    # Sentry
    sentry_dsn: str = ""

    # Claude API
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_model: str = "claude-sonnet-4-6-20250514"
    anthropic_max_concurrent: int = 10
    anthropic_rpm_limit: int = 50
    anthropic_tpm_limit: int = 200_000

    # Google Sheets
    google_service_account_path: str = ""

    # Upload limits
    max_upload_size_bytes: int = 10 * 1024 * 1024 * 1024  # 10GB
    max_image_size_bytes: int = 10 * 1024 * 1024  # 10MB per image
    max_images_per_asin: int = 10
    upload_dir: str = "./uploads"

    # AWS S3 (optional — if aws_s3_bucket is set, uploads go to S3 instead of local disk)
    aws_s3_bucket: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-1"

    # Processing
    celery_worker_max_tasks: int = 50
    retry_max_attempts: int = 3
    retry_base_delay: float = 2.0

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()


def setup_logging():
    import logging

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer()
            if settings.debug
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if settings.debug else logging.INFO,
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = __name__):
    return structlog.get_logger(name)

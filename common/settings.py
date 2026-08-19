import shutil
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ACQ_", env_file=".env")
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://acq:acq@localhost:5432/acq"
    document_root: Path = Path("documents")
    storage_backend: Literal["filesystem", "s3"] = "filesystem"
    s3_endpoint: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    session_secret: str = "development-only-secret"
    auth_password_hash: str | None = None
    read_only: bool = False
    secure_cookie: bool = True
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    cors_origins: str = ""
    analysis_pipeline: Literal["whole_pdf", "legacy"] = "whole_pdf"
    extraction_api_key: str | None = None
    extraction_base_url: str = "https://api.openai.com/v1"
    whole_pdf_model: str = "gpt-4o-mini"
    extraction_cheap_model: str = "gpt-4o-mini"
    extraction_frontier_model: str = "gpt-4o"
    extraction_timeout_seconds: float = 180.0
    extraction_max_retries: int = 3

    def validate_production(self) -> None:
        """Fail fast for deployment mistakes while keeping local tests frictionless."""
        if self.environment != "production":
            return
        missing: list[str] = []
        if self.session_secret == "development-only-secret":
            missing.append("ACQ_SESSION_SECRET")
        if not self.auth_password_hash:
            missing.append("ACQ_AUTH_PASSWORD_HASH")
        if "localhost" in self.database_url or "127.0.0.1" in self.database_url:
            missing.append("ACQ_DATABASE_URL")
        if self.storage_backend == "s3":
            for name, value in (("ACQ_S3_BUCKET", self.s3_bucket),
                                ("ACQ_S3_ACCESS_KEY_ID", self.s3_access_key_id),
                                ("ACQ_S3_SECRET_ACCESS_KEY", self.s3_secret_access_key)):
                if not value:
                    missing.append(name)
        if self.analysis_pipeline == "whole_pdf" and not self.extraction_api_key:
            missing.append("ACQ_EXTRACTION_API_KEY")
        if self.analysis_pipeline == "legacy" and not (shutil.which("ocrmypdf") or shutil.which("tesseract")):
            missing.append("legacy OCR backend")
        if missing:
            raise RuntimeError("missing production configuration: " + ", ".join(missing))


settings = Settings()
settings.validate_production()

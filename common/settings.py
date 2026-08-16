from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ACQ_", env_file=".env")
    database_url: str = "postgresql+psycopg://acq:acq@localhost:5432/acq"
    document_root: Path = Path("documents")
    session_secret: str = "development-only-secret"
    auth_password_hash: str | None = None
    read_only: bool = False
    secure_cookie: bool = True


settings = Settings()

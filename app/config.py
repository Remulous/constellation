from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("CRM_DATA_DIR", "data"))
    database_url: str = os.getenv("DATABASE_URL", "")
    app_password_hash: str = os.getenv("APP_PASSWORD_HASH", "")
    session_secret: str = os.getenv("SESSION_SECRET", "change-me-in-production")
    secure_cookies: bool = os.getenv("SECURE_COOKIES", "true").lower() == "true"
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "10"))
    max_minutes_upload_mb: int = int(os.getenv("MAX_MINUTES_UPLOAD_MB", "16"))
    obsidian_vault: str = os.getenv("OBSIDIAN_VAULT", "")
    public_url: str = os.getenv("PUBLIC_URL", "http://localhost:8000").rstrip("/")
    mcp_api_token: str = os.getenv("MCP_API_TOKEN", "")

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{self.data_dir / 'constellation.db'}"


settings = Settings()

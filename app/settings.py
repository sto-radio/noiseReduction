from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_token: str = Field(default="change-me")
    public_base_url: str = Field(default="http://localhost:8080")

    data_root: Path = Field(default=Path("/data"))
    input_dir: Path = Field(default=Path("/data/input"))
    output_dir: Path = Field(default=Path("/data/output"))
    work_dir: Path = Field(default=Path("/data/work"))

    deepfilter_command: str = Field(default="deepFilter")
    deepfilter_model: str = Field(default="DeepFilterNet3")
    deepfilter_postfilter: bool = Field(default=True)
    deepfilter_atten_lim_db: int | None = Field(default=None)

    mono_mixdown: bool = Field(default=True)
    processing_sample_rate: int = Field(default=48000)
    target_loudness_lufs: float = Field(default=-18.0)
    true_peak_dbtp: float = Field(default=-1.5)
    lra: float = Field(default=11.0)
    output_format: str = Field(default="wav")
    mp3_bitrate: str = Field(default="192k")
    aac_bitrate: str = Field(default="192k")

    max_upload_mb: int = Field(default=2048)
    process_timeout_seconds: int = Field(default=7200)
    request_timeout_seconds: int = Field(default=60)

    def ensure_directories(self) -> None:
        for directory in (self.input_dir, self.output_dir, self.work_dir):
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings

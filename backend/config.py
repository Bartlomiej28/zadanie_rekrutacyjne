from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ollama_base_url: str = "http://localhost:11434"
    ollama_router_model: str = "qwen2.5:1.5b"
    ollama_reasoning_model: str = "qwen3:8b"
    ollama_writer_model: str = "gemma3:12b"
    ollama_timeout_s: float = 240.0  # limit na jedno wywołanie modelu
    ollama_num_ctx: int = 8192  # domyślne 4096 ucina kontekst grafiku
    ollama_keep_alive: str = "10m"

    database_path: str = "kostecki_labs.db"
    shift_hours: int = 12
    base_monthly_hours: int = 160
    day_shift_required: int = 3
    night_shift_required: int = 2


settings = Settings()
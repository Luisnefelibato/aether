from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings


ROOT = Path(__file__).resolve().parents[3]


class IdentityConfig(BaseModel):
    short_name: str = "ADAM"
    expansion_en: str = "Automated Developer and Asset Manager"
    expansion_es: str = "Desarrollador automatizado y gestor de activos"
    reveal_meaning_only_when_asked: bool = True


class AudioConfig(BaseModel):
    input_device: int | None = None
    channels: int = 1
    chunk_ms: int = 30
    silence_ms: int = 1200
    max_utterance_ms: int = 60000
    wake_enabled: bool = True
    wake_phrases: list[str] = Field(
        default_factory=lambda: ["oye adam", "hey adam", "adam"]
    )
    wake_sensitivity: float = 0.55
    ptt_hotkey: str = "ctrl+alt+space"
    barge_in: bool = True


class SttConfig(BaseModel):
    provider: str = "elevenlabs"  # elevenlabs | whisper | auto
    model: str = "tiny"
    eleven_model: str = "scribe_v2"
    device: str = "cpu"
    compute_type: str = "int8"
    language: str = "es"


class BrainConfig(BaseModel):
    model: str = "kimi-k2.6"
    base_url: str = "https://api.moonshot.ai/v1"
    temperature: float = 1.0
    max_tokens: int = 2048
    max_tool_rounds: int = 8
    thinking: bool = False


class VoiceConfig(BaseModel):
    model_id: str = "eleven_multilingual_v2"
    stability: float = 0.45
    similarity_boost: float = 0.75
    style: float = 0.15
    speak_progress: bool = True


class HudConfig(BaseModel):
    ws_host: str = "127.0.0.1"
    ws_port: int = 8765
    autostart: bool = True


class MemoryConfig(BaseModel):
    sqlite_path: str = "data/aether.db"
    chroma_path: str = "data/chroma"
    embed_collection: str = "aether_semantic"
    session_turns: int = 40
    user_name: str = "Luisfer"
    address_as: str = "Luisfer"
    facts_collection: str = "adam_facts"
    fact_limit_recall: int = 12
    consolidation_interval_h: int = 24
    max_facts: int = 5000


class SupervisorConfig(BaseModel):
    max_concurrent_jobs: int = 4
    default_timeout_s: int = 120
    delegated_timeout_s: int = 1800
    inline_max_s: int = 15
    background_by_default: bool = True
    recover_stale_running_s: int = 60
    progress_throttle_ms: int = 750
    verify_after_cursor: bool = True
    default_verify_command: str = ""


class PolicyConfig(BaseModel):
    trusted_mode: bool = True
    default_level: str = "L0"
    require_oral_confirm_for: list[str] = Field(default_factory=list)
    blocked_patterns: list[str] = Field(
        default_factory=lambda: [
            r"(?i)password\s*=",
            r"(?i)api[_-]?key",
        ]
    )
    app_whitelist: list[str] = Field(
        default_factory=lambda: [
            "notepad",
            "calc",
            "explorer",
            "chrome",
            "msedge",
            "firefox",
            "code",
            "cursor",
            "windows terminal",
            "wt",
            "powershell",
            "cmd",
        ]
    )
    path_whitelist: list[str] = Field(
        default_factory=lambda: ["~/Documents", "~/Downloads", "~/Desktop", "~"]
    )
    always_confirm_tools: list[str] = Field(
        default_factory=lambda: [
            "mail.send",
            "gmail.send",
            "gmail.delete",
            "calendar.delete_event_google",
            "calendar.publish_event",
            "browser.drive_upload",
            "drive.share",
            "messaging.send_telegram",
            "messaging.send_whatsapp",
            "messaging.delete_message",
            "backup.restore",
            "commerce.checkout",
        ]
    )
    always_deny_tools: list[str] = Field(
        default_factory=lambda: ["system.shutdown", "system.format"]
    )
    critical_entity_prefixes: list[str] = Field(
        default_factory=lambda: ["lock.", "alarm_control_panel."]
    )
    max_visual_actions_per_minute: int = 30


class CapabilitiesConfig(BaseModel):
    desktop_win: bool = True
    dev_tools: bool = True
    browser: bool = True
    iot_home: bool = True
    calendar_mail: bool = True
    screen_camera: bool = True
    files_intel: bool = True
    backup: bool = True
    health: bool = True
    scheduler: bool = True
    messaging: bool = True
    integrations: bool = True


class IotConfig(BaseModel):
    home_assistant_url: str = ""
    home_assistant_token: str = ""
    mqtt_url: str = ""
    critical_entities: list[str] = Field(default_factory=list)


class CalendarMailConfig(BaseModel):
    provider: str = "local_stub"
    google_token_path: str = "data/google_tokens.json"


class BrowserConfig(BaseModel):
    headless: bool = False
    timeout_ms: int = 30000
    channel: str = "chrome"
    user_data_dir: str = "data/browser_profile"
    profile_directory: str = "Default"


class VisionConfig(BaseModel):
    default_monitor: int = 1
    max_width: int = 1280
    max_height: int = 720
    captures_dir: str = "data/captures"
    capture_ttl_h: int = 24
    grounding_min_confidence: float = 0.6
    settle_ms: int = 600


class FilesConfig(BaseModel):
    index_roots: list[str] = Field(
        default_factory=lambda: ["~/Documents", "~/Downloads", "~/Desktop"]
    )
    exclude_globs: list[str] = Field(
        default_factory=lambda: [
            "**/.git/**",
            "**/node_modules/**",
            "**/.venv/**",
        ]
    )
    max_index_files: int = 50000
    max_depth: int = 8
    duplicate_min_size_mb: int = 1
    trash_dir: str = "data/trash"
    sqlite_path: str = "data/aether.db"


class BackupConfig(BaseModel):
    default_dest: str = "data/backups"
    max_parallel: int = 1
    max_set_size_gb: int = 50
    retention_days: int = 30
    verify_after_write: bool = True
    sqlite_path: str = "data/aether.db"


class SchedulerConfig(BaseModel):
    tick_seconds: int = 30
    max_missed_catchup: int = 3
    routine_timeout_s: int = 600
    timezone: str = "America/Bogota"
    sqlite_path: str = "data/aether.db"
    poll_seconds: float = 30.0


class HealthConfig(BaseModel):
    sample_interval_s: int = 300
    retain_snapshots_days: int = 30
    disks: list[str] = Field(default_factory=lambda: ["C:\\"])
    cpu_warn_percent: float = 85.0
    memory_warn_percent: float = 80.0
    disk_free_warn_gb: float = 15.0
    alert_cooldown_s: int = 3600
    sqlite_path: str = "data/aether.db"


class GoogleConfig(BaseModel):
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = "http://127.0.0.1:8766/"
    token_path: str = "data/google_tokens.json"
    scopes: list[str] = Field(
        default_factory=lambda: [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/calendar.events",
        ]
    )


class SpotifyConfig(BaseModel):
    client_id: str = ""
    redirect_uri: str = "http://127.0.0.1:8080/"
    token_path: str = "data/spotify_tokens.json"


class MessagingConfig(BaseModel):
    telegram_bot_token: str = ""
    whatsapp_cloud_token: str = ""
    whatsapp_phone_number_id: str = ""


class Settings(BaseSettings):
    name: str = "ADAM"
    language: str = "es"
    sample_rate: int = 16000
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    stt: SttConfig = Field(default_factory=SttConfig)
    brain: BrainConfig = Field(default_factory=BrainConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    hud: HudConfig = Field(default_factory=HudConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    capabilities: CapabilitiesConfig = Field(default_factory=CapabilitiesConfig)
    iot: IotConfig = Field(default_factory=IotConfig)
    calendar_mail: CalendarMailConfig = Field(default_factory=CalendarMailConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    files: FilesConfig = Field(default_factory=FilesConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    google: GoogleConfig = Field(default_factory=GoogleConfig)
    spotify: SpotifyConfig = Field(default_factory=SpotifyConfig)
    messaging: MessagingConfig = Field(default_factory=MessagingConfig)

    moonshot_api_key: str = ""
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    cursor_api_key: str = ""

    model_config = ConfigDict(env_file=".env", extra="ignore")

    def resolve_path(self, relative: str) -> Path:
        path = Path(relative)
        if path.is_absolute():
            return path
        return ROOT / path


def load_settings(config_path: str | Path | None = None) -> Settings:
    load_dotenv(ROOT / ".env")
    path = Path(config_path or os.getenv("AETHER_CONFIG", "configs/aether.yaml"))
    if not path.is_absolute():
        path = ROOT / path

    raw: dict[str, Any] = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

    raw["moonshot_api_key"] = (
        os.getenv("MOONSHOT_API_KEY")
        or os.getenv("KIMI_API_KEY")
        or raw.get("moonshot_api_key", "")
    )
    raw["elevenlabs_api_key"] = os.getenv(
        "ELEVENLABS_API_KEY", raw.get("elevenlabs_api_key", "")
    )
    raw["elevenlabs_voice_id"] = os.getenv(
        "ELEVENLABS_VOICE_ID",
        raw.get("elevenlabs_voice_id", "21m00Tcm4TlvDq8ikWAM"),
    )
    raw["cursor_api_key"] = os.getenv(
        "CURSOR_API_KEY", raw.get("cursor_api_key", "")
    )
    raw.setdefault("iot", {})
    raw["iot"]["home_assistant_url"] = os.getenv(
        "HOME_ASSISTANT_URL", raw["iot"].get("home_assistant_url", "")
    )
    raw["iot"]["home_assistant_token"] = os.getenv(
        "HOME_ASSISTANT_TOKEN", raw["iot"].get("home_assistant_token", "")
    )
    raw.setdefault("google", {})
    raw["google"]["client_id"] = os.getenv(
        "GOOGLE_CLIENT_ID", raw["google"].get("client_id", "")
    )
    raw["google"]["client_secret"] = os.getenv(
        "GOOGLE_CLIENT_SECRET", raw["google"].get("client_secret", "")
    )
    raw["google"]["redirect_uri"] = os.getenv(
        "GOOGLE_REDIRECT_URI",
        raw["google"].get(
            "redirect_uri", "http://127.0.0.1:8766/"
        ),
    )
    raw.setdefault("spotify", {})
    raw["spotify"]["client_id"] = os.getenv(
        "SPOTIFY_CLIENT_ID", raw["spotify"].get("client_id", "")
    )
    raw["spotify"]["redirect_uri"] = os.getenv(
        "SPOTIFY_REDIRECT_URI",
        raw["spotify"].get("redirect_uri", "http://127.0.0.1:8080/"),
    )
    raw.setdefault("messaging", {})
    raw["messaging"]["telegram_bot_token"] = os.getenv(
        "TELEGRAM_BOT_TOKEN", raw["messaging"].get("telegram_bot_token", "")
    )
    raw["messaging"]["whatsapp_cloud_token"] = os.getenv(
        "WHATSAPP_CLOUD_TOKEN", raw["messaging"].get("whatsapp_cloud_token", "")
    )
    raw["messaging"]["whatsapp_phone_number_id"] = os.getenv(
        "WHATSAPP_PHONE_NUMBER_ID",
        raw["messaging"].get("whatsapp_phone_number_id", ""),
    )
    return Settings(**raw)

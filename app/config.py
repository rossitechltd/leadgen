import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(key: str, default: bool = False) -> bool:
    value = os.getenv(key, str(default)).lower()
    return value in ("1", "true", "yes", "on")


def _env_int_or_none(key: str) -> int | None:
    raw = os.getenv(key, "").strip()
    if not raw:
        return None
    return int(raw)


@dataclass(frozen=True)
class Settings:
    google_sheet_name: str
    google_service_account_file: str
    sheet_dynamic_lead: str
    sheet_all_imported: str
    sheet_ready_to_contact: str
    sheet_scrape_queue: str
    scrape_min_length: int
    scrape_queue_delay_secs: float
    scrape_max_attempts: int
    scrape_max_failures: int
    scrape_stable_secs: float
    scrape_min_start_secs: float
    scrape_stall_secs: float
    page_scrape_poll_secs: float
    scrape_active_poll_secs: float
    scrape_state_path: Path
    scrape_queue_poll_enabled: bool
    pipeline_run_time: str
    pipeline_enabled: bool
    host: str
    port: int
    apify_api_token: str
    apify_actor_id: str
    apify_proxy_country: str
    apify_proxy_groups: tuple[str, ...]
    apify_min_delay: int
    apify_max_delay: int
    apify_member_count: int | None
    fb_cookies_file: str
    fb_groups_file: str
    refine_batch_size: int
    openrouter_api_key: str
    openrouter_model: str
    openrouter_base_url: str
    qualify_website_timeout_secs: float
    qualify_max_redirects: int
    qualify_fetch_retries: int
    entity_classify_batch_size: int
    entity_screen_auto_person: float
    entity_classify_auto_person: float
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_poll_ms: int
    dashboard_url_override: str
    operator_attention_path: Path
    step1_checkpoint_path: Path

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token.strip() and self.telegram_chat_id.strip())

    @property
    def dashboard_url(self) -> str:
        override = (self.dashboard_url_override or "").strip()
        if override:
            return override.rstrip("/")
        return f"http://{self.host}:{self.port}"

    @property
    def openrouter_configured(self) -> bool:
        return bool(self.openrouter_api_key.strip())

    @property
    def service_account_path(self) -> Path:
        path = Path(self.google_service_account_file)
        if not path.is_absolute():
            path = BASE_DIR / path
        return path

    @property
    def fb_cookies_path(self) -> Path:
        path = Path(self.fb_cookies_file)
        if not path.is_absolute():
            path = BASE_DIR / path
        return path

    @property
    def fb_groups_path(self) -> Path:
        path = Path(self.fb_groups_file)
        if not path.is_absolute():
            path = BASE_DIR / path
        return path

    @property
    def sheets_configured(self) -> bool:
        return self.service_account_path.exists()

    @property
    def apify_configured(self) -> bool:
        return bool(self.apify_api_token.strip())

    @property
    def fb_cookies_configured(self) -> bool:
        return self.fb_cookies_path.exists()


@lru_cache
def get_settings() -> Settings:
    return Settings(
        google_sheet_name=os.getenv("GOOGLE_SHEET_NAME", "Lead Manager"),
        google_service_account_file=os.getenv(
            "GOOGLE_SERVICE_ACCOUNT_FILE", "autoleadverification-e76d53033380.json"
        ),
        sheet_dynamic_lead=os.getenv("SHEET_DYNAMIC_LEAD", "Dynamic Lead Sheet"),
        sheet_all_imported=os.getenv("SHEET_ALL_IMPORTED", "allimported"),
        sheet_ready_to_contact=os.getenv("SHEET_READY_TO_CONTACT", "Ready to Contact"),
        sheet_scrape_queue=os.getenv("SHEET_SCRAPE_QUEUE", "scrapesheet"),
        scrape_min_length=int(os.getenv("SCRAPE_MIN_LENGTH", "50")),
        scrape_queue_delay_secs=float(os.getenv("SCRAPE_QUEUE_DELAY_SECS", "3")),
        scrape_max_attempts=int(os.getenv("SCRAPE_MAX_ATTEMPTS", "2")),
        scrape_max_failures=int(os.getenv("SCRAPE_MAX_FAILURES", "3")),
        scrape_stable_secs=float(os.getenv("SCRAPE_STABLE_SECS", "2")),
        scrape_min_start_secs=float(os.getenv("SCRAPE_MIN_START_SECS", "3")),
        scrape_stall_secs=max(float(os.getenv("SCRAPE_STALL_SECS", "120")), 90.0),
        page_scrape_poll_secs=float(os.getenv("PAGE_SCRAPE_POLL_SECS", "20")),
        scrape_active_poll_secs=float(os.getenv("SCRAPE_ACTIVE_POLL_SECS", "3")),
        scrape_state_path=BASE_DIR
        / os.getenv("SCRAPE_STATE_FILE", "data/scrape_queue/active.json"),
        scrape_queue_poll_enabled=_env_bool("SCRAPE_QUEUE_POLL_ENABLED", True),
        pipeline_run_time=os.getenv("PIPELINE_RUN_TIME", "09:00"),
        pipeline_enabled=_env_bool("PIPELINE_ENABLED", True),
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        apify_api_token=os.getenv("APIFY_API_TOKEN", ""),
        apify_actor_id=os.getenv(
            "APIFY_ACTOR_ID", "curious_coder/facebook-group-member-scraper"
        ),
        apify_proxy_country=os.getenv("APIFY_PROXY_COUNTRY", "GB"),
        apify_proxy_groups=tuple(
            g.strip()
            for g in os.getenv("APIFY_PROXY_GROUPS", "").split(",")
            if g.strip() and g.strip().lower() not in {"none", "datacenter", "default"}
        ),
        apify_min_delay=int(os.getenv("APIFY_MIN_DELAY", "1")),
        apify_max_delay=int(os.getenv("APIFY_MAX_DELAY", "10")),
        apify_member_count=_env_int_or_none("APIFY_MEMBER_COUNT") or 20,
        fb_cookies_file=os.getenv("FB_COOKIES_FILE", "credentials/fb_cookies.json"),
        fb_groups_file=os.getenv("FB_GROUPS_FILE", "config/groups.json"),
        refine_batch_size=int(os.getenv("REFINE_BATCH_SIZE", "25")),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
        openrouter_model=os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
        openrouter_base_url=os.getenv(
            "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
        ),
        qualify_website_timeout_secs=float(
            os.getenv("QUALIFY_WEBSITE_TIMEOUT_SECS", "15")
        ),
        qualify_max_redirects=int(os.getenv("QUALIFY_MAX_REDIRECTS", "10")),
        qualify_fetch_retries=int(os.getenv("QUALIFY_FETCH_RETRIES", "3")),
        entity_classify_batch_size=int(os.getenv("ENTITY_CLASSIFY_BATCH_SIZE", "10")),
        entity_screen_auto_person=float(os.getenv("ENTITY_SCREEN_AUTO_PERSON", "0.88")),
        entity_classify_auto_person=float(
            os.getenv("ENTITY_CLASSIFY_AUTO_PERSON", "0.75")
        ),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        telegram_poll_ms=int(os.getenv("TELEGRAM_POLL_MS", "60000")),
        dashboard_url_override=os.getenv("DASHBOARD_URL", ""),
        operator_attention_path=BASE_DIR
        / os.getenv("OPERATOR_ATTENTION_FILE", "data/operator_attention.json"),
        step1_checkpoint_path=BASE_DIR
        / os.getenv("STEP1_CHECKPOINT_FILE", "data/step1_checkpoint.json"),
    )

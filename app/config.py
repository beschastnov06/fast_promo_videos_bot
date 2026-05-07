from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    bot_token: str
    openai_api_key: str | None
    telegram_api_base: str | None
    telegram_api_is_local: bool
    restrict_telegram_users: bool
    allowed_telegram_usernames: frozenset[str]
    database_url: str | None
    redis_url: str | None
    tmp_dir: Path
    max_concurrent_renders: int
    render_job_timeout_seconds: int
    telegram_request_timeout_seconds: int
    public_base_url: str | None
    robokassa_merchant_login: str | None
    robokassa_password1: str | None
    robokassa_password2: str | None
    robokassa_test_password1: str | None
    robokassa_test_password2: str | None
    robokassa_test_mode: bool
    robokassa_hash_algorithm: str
    robokassa_offer_url: str


def load_config() -> Config:
    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is not set. Add it to .env or environment variables.")

    return Config(
        bot_token=bot_token,
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        telegram_api_base=os.getenv("TELEGRAM_API_BASE"),
        telegram_api_is_local=os.getenv("TELEGRAM_API_IS_LOCAL", "").lower() in {"1", "true", "yes"},
        restrict_telegram_users=_parse_bool(os.getenv("RESTRICT_TELEGRAM_USERS"), default=False),
        allowed_telegram_usernames=_parse_usernames(os.getenv("ALLOWED_TELEGRAM_USERNAMES", "")),
        database_url=os.getenv("DATABASE_URL"),
        redis_url=os.getenv("REDIS_URL"),
        tmp_dir=Path(os.getenv("TMP_DIR", "tmp")),
        max_concurrent_renders=_parse_int(os.getenv("MAX_CONCURRENT_RENDERS"), default=1, minimum=1),
        render_job_timeout_seconds=_parse_int(os.getenv("RENDER_JOB_TIMEOUT_SECONDS"), default=900, minimum=900),
        telegram_request_timeout_seconds=_parse_int(
            os.getenv("TELEGRAM_REQUEST_TIMEOUT_SECONDS"),
            default=600,
            minimum=60,
        ),
        public_base_url=_normalize_url(os.getenv("PUBLIC_BASE_URL")),
        robokassa_merchant_login=os.getenv("ROBOKASSA_MERCHANT_LOGIN"),
        robokassa_password1=os.getenv("ROBOKASSA_PASSWORD1"),
        robokassa_password2=os.getenv("ROBOKASSA_PASSWORD2"),
        robokassa_test_password1=os.getenv("ROBOKASSA_TEST_PASSWORD1"),
        robokassa_test_password2=os.getenv("ROBOKASSA_TEST_PASSWORD2"),
        robokassa_test_mode=_parse_bool(os.getenv("ROBOKASSA_TEST_MODE"), default=False),
        robokassa_hash_algorithm=os.getenv("ROBOKASSA_HASH_ALGORITHM", "md5").strip().lower(),
        robokassa_offer_url=os.getenv(
            "ROBOKASSA_OFFER_URL",
            "https://telegra.ph/Oferta-usloviya-okazaniya-uslug-i-politika-obrabotki-personalnyh-dannyh-05-07",
        ).strip(),
    )


def _parse_usernames(value: str) -> frozenset[str]:
    usernames = {
        username.strip().removeprefix("@").casefold()
        for username in value.split(",")
        if username.strip()
    }
    return frozenset(usernames)


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default

    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _parse_int(value: str | None, default: int, minimum: int | None = None) -> int:
    if value is None or not value.strip():
        return default

    try:
        parsed_value = int(value)
    except ValueError:
        return default

    if minimum is not None:
        return max(parsed_value, minimum)

    return parsed_value


def _normalize_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None

    return value.strip().rstrip("/")

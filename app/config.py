from dataclasses import dataclass
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    bot_token: str
    openai_api_key: str | None
    telegram_api_base: str | None
    telegram_api_is_local: bool
    allowed_telegram_usernames: frozenset[str]


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
        allowed_telegram_usernames=_parse_usernames(os.getenv("ALLOWED_TELEGRAM_USERNAMES", "")),
    )


def _parse_usernames(value: str) -> frozenset[str]:
    usernames = {
        username.strip().removeprefix("@").casefold()
        for username in value.split(",")
        if username.strip()
    }
    return frozenset(usernames)

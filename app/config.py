from dataclasses import dataclass
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    bot_token: str
    telegram_api_base: str | None
    telegram_api_is_local: bool


def load_config() -> Config:
    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is not set. Add it to .env or environment variables.")

    return Config(
        bot_token=bot_token,
        telegram_api_base=os.getenv("TELEGRAM_API_BASE"),
        telegram_api_is_local=os.getenv("TELEGRAM_API_IS_LOCAL", "").lower() in {"1", "true", "yes"},
    )

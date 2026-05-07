from __future__ import annotations

import html
import logging
import os
from typing import Any

from aiohttp import web
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from app.billing.packages import package_by_code
from app.billing.robokassa import (
    RobokassaConfigError,
    RobokassaSignatureError,
    build_payment_form,
    extract_shp_params,
    fail_url,
    success_url,
    verify_result_signature,
)
from app.config import Config, load_config
from app.db import create_engine, create_session_factory
from app.repositories.payments import get_payment_by_invoice_id, mark_payment_paid_and_credit


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def robokassa_pay(request: web.Request) -> web.Response:
    config: Config = request.app["config"]
    session_factory = request.app["session_factory"]
    invoice_id = _invoice_id(request)

    async with session_factory() as session:
        payment = await get_payment_by_invoice_id(session, invoice_id)
        if payment is None:
            raise web.HTTPNotFound(text="Payment not found")
        if payment.status != "pending":
            return _html_response(_simple_page("Счет уже обработан", "Вернитесь в Telegram."))

        package = package_by_code(payment.package_code)
        if package is None:
            raise web.HTTPNotFound(text="Payment package not found")
        form = build_payment_form(config, payment, package)

    return _html_response(_auto_submit_form(form.action_url, form.fields))


async def robokassa_result(request: web.Request) -> web.Response:
    config: Config = request.app["config"]
    session_factory = request.app["session_factory"]
    bot: Bot = request.app["bot"]

    params = await _request_params(request)
    logger.info("Received Robokassa ResultURL: %s", _safe_log_params(params))

    out_sum = params.get("OutSum")
    inv_id = params.get("InvId")
    signature = params.get("SignatureValue")
    if not out_sum or not inv_id or not signature:
        raise web.HTTPBadRequest(text="Missing required Robokassa params")

    shp_params = extract_shp_params(params)
    try:
        verify_result_signature(
            config,
            out_sum=out_sum,
            inv_id=inv_id,
            signature_value=signature,
            shp_params=shp_params,
        )
    except (RobokassaConfigError, RobokassaSignatureError):
        logger.exception("Robokassa signature verification failed: inv_id=%s", inv_id)
        raise web.HTTPBadRequest(text="Invalid signature") from None

    try:
        invoice_id = int(inv_id)
    except ValueError:
        raise web.HTTPBadRequest(text="Invalid InvId") from None

    async with session_factory() as session:
        async with session.begin():
            payment = await get_payment_by_invoice_id(session, invoice_id, for_update=True)
            if payment is None:
                raise web.HTTPNotFound(text="Payment not found")
            if _amount_cents(out_sum) != payment.amount_cents:
                logger.error(
                    "Robokassa amount mismatch: invoice_id=%s expected=%s got=%s",
                    invoice_id,
                    payment.amount_cents,
                    out_sum,
                )
                raise web.HTTPBadRequest(text="Amount mismatch")
            credited, balance_value = await mark_payment_paid_and_credit(
                session,
                payment,
                buyer_email=params.get("EMail") or params.get("Email"),
                raw_provider_payload=params,
            )
            chat_id = payment.telegram_chat_id
            invoice_message_id = payment.telegram_invoice_message_id
            credits_amount = payment.credits_amount

    if credited:
        await _delete_invoice_message(
            bot=bot,
            chat_id=chat_id,
            message_id=invoice_message_id,
        )
        await _send_payment_success_message(
            bot=bot,
            chat_id=chat_id,
            credits_amount=credits_amount,
            balance_value=balance_value,
        )

    return web.Response(text=f"OK{invoice_id}", content_type="text/plain")


async def robokassa_success(request: web.Request) -> web.Response:
    return _html_response(
        _simple_page(
            "Оплата подтверждается",
            "Вернитесь в Telegram. Баланс пополнится после подтверждения платежа Robokassa.",
        )
    )


async def robokassa_fail(request: web.Request) -> web.Response:
    return _html_response(
        _simple_page(
            "Оплата не завершена",
            "Платеж отменен или не прошел. Вернитесь в Telegram и попробуйте еще раз.",
        )
    )


async def startup(app: web.Application) -> None:
    config = load_config()
    engine = create_engine(config)
    session_factory = create_session_factory(engine)
    bot = _create_bot(config)

    # Validate URLs early so deployment fails loudly when Robokassa settings are incomplete.
    success_url(config)
    fail_url(config)

    app["config"] = config
    app["engine"] = engine
    app["session_factory"] = session_factory
    app["bot"] = bot
    logger.info("Web service started")


async def cleanup(app: web.Application) -> None:
    bot: Bot | None = app.get("bot")
    engine = app.get("engine")
    if bot:
        await bot.session.close()
    if engine:
        await engine.dispose()
    logger.info("Web service stopped")


def create_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    app.router.add_get("/health", health)
    app.router.add_get("/robokassa/pay/{invoice_id}", robokassa_pay)
    app.router.add_post("/robokassa/result", robokassa_result)
    app.router.add_get("/robokassa/success", robokassa_success)
    app.router.add_post("/robokassa/success", robokassa_success)
    app.router.add_get("/robokassa/fail", robokassa_fail)
    app.router.add_post("/robokassa/fail", robokassa_fail)
    return app


def main() -> None:
    web.run_app(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


def _create_bot(config: Config) -> Bot:
    session = _create_session(config)
    return Bot(token=config.bot_token, session=session) if session else Bot(token=config.bot_token)


def _create_session(config: Config) -> AiohttpSession | None:
    if not config.telegram_api_base:
        return AiohttpSession(timeout=config.telegram_request_timeout_seconds)

    api = TelegramAPIServer.from_base(
        config.telegram_api_base,
        is_local=config.telegram_api_is_local,
    )
    return AiohttpSession(api=api, timeout=config.telegram_request_timeout_seconds)


def _invoice_id(request: web.Request) -> int:
    try:
        return int(request.match_info["invoice_id"])
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(text="Invalid invoice id") from None


async def _request_params(request: web.Request) -> dict[str, str]:
    if request.method == "POST":
        data = await request.post()
        return {key: str(value) for key, value in data.items()}

    return {key: value for key, value in request.query.items()}


def _amount_cents(out_sum: str) -> int:
    normalized = out_sum.replace(",", ".")
    whole, _, fraction = normalized.partition(".")
    fraction = (fraction + "00")[:2]
    return int(whole) * 100 + int(fraction)


async def _send_payment_success_message(
    *,
    bot: Bot,
    chat_id: int,
    credits_amount: int,
    balance_value: int,
) -> None:
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "Оплата прошла ✅\n"
            f"Начислено: {credits_amount} видео\n"
            f"Баланс: {balance_value} видео\n\n"
            "На email может прийти письмо от Robokassa с подтверждением оплаты.\n\n"
            "Кассовый чек придет отдельным письмом на тот же email.\n"
            "Срок формирования чека — до 24 часов."
        ),
    )


async def _delete_invoice_message(
    *,
    bot: Bot,
    chat_id: int,
    message_id: int | None,
) -> None:
    if message_id is None:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        logger.warning(
            "Failed to delete Robokassa invoice message: chat_id=%s message_id=%s",
            chat_id,
            message_id,
            exc_info=True,
        )


def _auto_submit_form(action_url: str, fields: dict[str, str]) -> str:
    inputs = "\n".join(
        (
            f'<input type="hidden" name="{html.escape(name, quote=True)}" '
            f'value="{html.escape(value, quote=True)}">'
        )
        for name, value in fields.items()
    )
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Переход к оплате</title>
</head>
<body>
  <p>Переходим к оплате...</p>
  <form id="pay" method="POST" action="{html.escape(action_url, quote=True)}">
    {inputs}
    <noscript><button type="submit">Перейти к оплате</button></noscript>
  </form>
  <script>document.getElementById("pay").submit();</script>
</body>
</html>"""


def _simple_page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  <p>{html.escape(body)}</p>
  <p><a href="https://t.me/fast_promo_videos_bot">Вернуться в бот</a></p>
</body>
</html>"""


def _html_response(value: str) -> web.Response:
    return web.Response(text=value, content_type="text/html")


def _safe_log_params(params: dict[str, str]) -> dict[str, Any]:
    return {
        key: ("***" if key.casefold() in {"signaturevalue"} else value)
        for key, value in params.items()
    }


if __name__ == "__main__":
    main()

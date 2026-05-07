from __future__ import annotations

import html
import logging
import os
from pathlib import Path
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
STATIC_DIR = Path(__file__).with_name("static")
BOT_URL = "https://t.me/fast_promo_videos_bot"


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def robokassa_pay(request: web.Request) -> web.Response:
    config: Config = request.app["config"]
    session_factory = request.app["session_factory"]
    invoice_id = _invoice_id(request)

    async with session_factory() as session:
        payment = await get_payment_by_invoice_id(session, invoice_id)
        if payment is None:
            return await robokassa_not_found(request)
        if payment.status != "pending":
            return await robokassa_processed_payment(request)

        package = package_by_code(payment.package_code)
        if package is None:
            return await robokassa_not_found(request)
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
        _payment_status_page(
            title="Оплата прошла",
            heading="Оплата прошла",
            status_icon="✓",
            status="success",
            body=(
                "Мы уже проверяем платеж. Если оплата подтверждена Robokassa, "
                "видео будут начислены на баланс в Telegram."
            ),
            note=(
                "Кассовый чек придет отдельным письмом на email, указанный при оплате. "
                "Срок формирования чека — до 24 часов."
            ),
            footer="Если баланс не обновился в течение нескольких минут, напишите в поддержку через бота.",
        )
    )


async def robokassa_fail(request: web.Request) -> web.Response:
    return _html_response(
        _payment_status_page(
            title="Оплата не завершена",
            heading="Оплата не завершена",
            status_icon="!",
            status="fail",
            body=(
                "Платеж был отменен или не прошел. Вы можете вернуться в Telegram "
                "и попробовать оплатить пакет еще раз."
            ),
            note="Если деньги списались, но баланс не пополнился, напишите в поддержку через бота.",
            footer="Баланс обновляется только после подтверждения платежа Robokassa.",
        )
    )


async def robokassa_processed_payment(request: web.Request) -> web.Response:
    return _html_response(
        _payment_status_page(
            title="Счет уже обработан",
            heading="Счет уже обработан",
            status_icon="✓",
            status="success",
            body="Этот счет уже был обработан. Вернитесь в Telegram, чтобы проверить баланс.",
            note=(
                "Если баланс не обновился, напишите в поддержку через бота "
                "и укажите номер операции Robokassa."
            ),
            footer="Повторная оплата по этому счету недоступна.",
        )
    )


async def robokassa_not_found(request: web.Request) -> web.Response:
    return _html_response(
        _payment_status_page(
            "Оплата не завершена",
            heading="Счет не найден",
            status_icon="!",
            status="fail",
            body="Не удалось найти счет на оплату. Вернитесь в Telegram и попробуйте создать новый счет.",
            note="Если проблема повторяется, напишите в поддержку через бота.",
            footer="Платеж не может быть продолжен без корректного счета.",
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
    app.router.add_static("/static/", STATIC_DIR, show_index=False)
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


def _payment_status_page(
    title: str,
    *,
    heading: str,
    status_icon: str,
    status: str,
    body: str,
    note: str,
    footer: str,
) -> str:
    status_class = "success" if status == "success" else "fail"
    safe_title = html.escape(title)
    safe_heading = html.escape(heading)
    safe_icon = html.escape(status_icon)
    safe_body = html.escape(body)
    safe_note = html.escape(note)
    safe_footer = html.escape(footer)
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title} — Fast Promo Videos Bot</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4f7fb;
      --panel: #ffffff;
      --text: #17202a;
      --muted: #657386;
      --line: #dbe4ef;
      --blue: #2388ff;
      --blue-dark: #126fe0;
      --green: #16a36a;
      --red: #d64b4b;
      font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: radial-gradient(circle at top left, rgba(35, 136, 255, 0.12), transparent 34rem), linear-gradient(180deg, #ffffff 0%, var(--bg) 72%);
      color: var(--text);
    }}
    .page {{
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 48px 20px;
    }}
    .screen {{
      width: min(620px, 100%);
      min-height: 620px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 34px;
      box-shadow: 0 24px 70px rgba(27, 39, 54, 0.12);
      display: flex;
      flex-direction: column;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 12px;
      color: #2a3645;
      font-size: 16px;
      font-weight: 700;
      margin-bottom: auto;
    }}
    .logo {{
      width: 46px;
      height: 46px;
      border-radius: 14px;
      object-fit: cover;
      box-shadow: 0 8px 20px rgba(22, 34, 51, 0.16);
    }}
    .content {{
      text-align: center;
      padding: 44px 0 36px;
    }}
    .status-icon {{
      width: 72px;
      height: 72px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      margin: 0 auto 22px;
      font-size: 34px;
      font-weight: 800;
    }}
    .success .status-icon {{
      background: rgba(22, 163, 106, 0.12);
      color: var(--green);
    }}
    .fail .status-icon {{
      background: rgba(214, 75, 75, 0.12);
      color: var(--red);
    }}
    h1 {{
      margin: 0 0 16px;
      font-size: clamp(30px, 6vw, 44px);
      line-height: 1.12;
      letter-spacing: 0;
    }}
    p {{
      margin: 0;
      color: var(--muted);
      font-size: 18px;
      line-height: 1.5;
    }}
    .note {{
      margin: 24px auto 0;
      max-width: 480px;
      padding: 16px 18px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: #f8fbff;
      color: #4d5c70;
      font-size: 15px;
      line-height: 1.45;
    }}
    .actions {{
      margin-top: 30px;
      display: flex;
      justify-content: center;
    }}
    .button {{
      min-height: 52px;
      padding: 0 24px;
      border-radius: 12px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      text-decoration: none;
      background: var(--blue);
      color: white;
      font-size: 17px;
      font-weight: 700;
      box-shadow: 0 12px 22px rgba(35, 136, 255, 0.28);
    }}
    .button:hover {{ background: var(--blue-dark); }}
    .support {{
      margin-top: auto;
      border-top: 1px solid var(--line);
      padding-top: 20px;
      color: #718093;
      font-size: 14px;
      line-height: 1.45;
      text-align: center;
    }}
    @media (max-width: 540px) {{
      .page {{ padding: 20px 12px; }}
      .screen {{
        min-height: calc(100vh - 40px);
        border-radius: 16px;
        padding: 24px 18px;
      }}
      .brand {{ font-size: 15px; }}
      .content {{ padding: 32px 0 28px; }}
      p {{ font-size: 16px; }}
      .button {{ width: 100%; }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <section class="screen {status_class}">
      <div class="brand">
        <img class="logo" src="/static/fast-promo-logo.png" alt="">
        <span>Fast Promo Videos Bot</span>
      </div>
      <div class="content">
        <div class="status-icon">{safe_icon}</div>
        <h1>{safe_heading}</h1>
        <p>{safe_body}</p>
        <div class="note">{safe_note}</div>
        <div class="actions">
          <a class="button" href="{BOT_URL}">Вернуться в Telegram</a>
        </div>
      </div>
      <div class="support">{safe_footer}</div>
    </section>
  </main>
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

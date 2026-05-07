from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
from urllib.parse import quote

from app.billing.packages import VideoPackage
from app.config import Config
from app.models import Payment


ROBOKASSA_PAYMENT_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"


class RobokassaConfigError(RuntimeError):
    pass


class RobokassaSignatureError(RuntimeError):
    pass


@dataclass(frozen=True)
class RobokassaPaymentForm:
    action_url: str
    fields: dict[str, str]


def ensure_robokassa_config(config: Config) -> None:
    missing = []
    if not config.public_base_url:
        missing.append("PUBLIC_BASE_URL")
    if not config.robokassa_merchant_login:
        missing.append("ROBOKASSA_MERCHANT_LOGIN")
    if not _password1(config):
        missing.append("ROBOKASSA_TEST_PASSWORD1" if config.robokassa_test_mode else "ROBOKASSA_PASSWORD1")
    if not _password2(config):
        missing.append("ROBOKASSA_TEST_PASSWORD2" if config.robokassa_test_mode else "ROBOKASSA_PASSWORD2")

    if missing:
        raise RobokassaConfigError(f"Missing Robokassa config: {', '.join(missing)}")

    _hash_func(config.robokassa_hash_algorithm)


def build_receipt(package: VideoPackage) -> str:
    receipt = {
        "sno": "nps",
        "items": [
            {
                "name": package.receipt_name,
                "quantity": 1,
                "sum": package.price_rub,
                "payment_method": "full_payment",
                "payment_object": "service",
                "tax": "none",
            }
        ],
    }
    raw_receipt = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))
    return quote(raw_receipt, safe="")


def build_payment_form(config: Config, payment: Payment, package: VideoPackage) -> RobokassaPaymentForm:
    ensure_robokassa_config(config)

    receipt = build_receipt(package)
    out_sum = _out_sum_from_cents(payment.amount_cents)
    inv_id = str(payment.invoice_id)
    description = package.receipt_name[:100]
    shp_params = {
        "Shp_package": package.code,
        "Shp_payment": str(payment.id),
        "Shp_user": str(payment.user_id),
    }
    signature = _payment_signature(
        config=config,
        out_sum=out_sum,
        inv_id=inv_id,
        receipt=receipt,
        shp_params=shp_params,
    )
    fields = {
        "MerchantLogin": _required(config.robokassa_merchant_login, "ROBOKASSA_MERCHANT_LOGIN"),
        "OutSum": out_sum,
        "InvId": inv_id,
        "Description": description,
        "SignatureValue": signature,
        "Receipt": receipt,
        "Culture": "ru",
        "Encoding": "utf-8",
        "IsTest": "1" if config.robokassa_test_mode else "0",
        **shp_params,
    }
    return RobokassaPaymentForm(action_url=ROBOKASSA_PAYMENT_URL, fields=fields)


def verify_result_signature(
    config: Config,
    *,
    out_sum: str,
    inv_id: str,
    signature_value: str,
    shp_params: dict[str, str],
) -> None:
    ensure_robokassa_config(config)
    expected = _result_signature(
        config=config,
        out_sum=out_sum,
        inv_id=inv_id,
        shp_params=shp_params,
    )
    if not hmac.compare_digest(expected.casefold(), signature_value.casefold()):
        raise RobokassaSignatureError("Invalid Robokassa ResultURL signature")


def extract_shp_params(params: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in params.items()
        if key.startswith("Shp_")
    }


def payment_page_url(config: Config, invoice_id: int) -> str:
    ensure_robokassa_config(config)
    return f"{config.public_base_url}/robokassa/pay/{invoice_id}"


def success_url(config: Config) -> str:
    ensure_robokassa_config(config)
    return f"{config.public_base_url}/robokassa/success"


def fail_url(config: Config) -> str:
    ensure_robokassa_config(config)
    return f"{config.public_base_url}/robokassa/fail"


def _payment_signature(
    *,
    config: Config,
    out_sum: str,
    inv_id: str,
    receipt: str,
    shp_params: dict[str, str],
) -> str:
    password1 = _required(
        _password1(config),
        "ROBOKASSA_TEST_PASSWORD1" if config.robokassa_test_mode else "ROBOKASSA_PASSWORD1",
    )
    parts = [
        _required(config.robokassa_merchant_login, "ROBOKASSA_MERCHANT_LOGIN"),
        out_sum,
        inv_id,
        receipt,
        password1,
    ]
    signature_base = ":".join(parts) + _shp_signature_suffix(shp_params)
    return _hash(signature_base, config.robokassa_hash_algorithm)


def _result_signature(
    *,
    config: Config,
    out_sum: str,
    inv_id: str,
    shp_params: dict[str, str],
) -> str:
    password2 = _required(
        _password2(config),
        "ROBOKASSA_TEST_PASSWORD2" if config.robokassa_test_mode else "ROBOKASSA_PASSWORD2",
    )
    signature_base = ":".join([out_sum, inv_id, password2]) + _shp_signature_suffix(shp_params)
    return _hash(signature_base, config.robokassa_hash_algorithm)


def _shp_signature_suffix(shp_params: dict[str, str]) -> str:
    if not shp_params:
        return ""
    return "".join(
        f":{key}={shp_params[key]}"
        for key in sorted(shp_params)
    )


def _hash(value: str, algorithm: str) -> str:
    return _hash_func(algorithm)(value.encode("utf-8")).hexdigest()


def _hash_func(algorithm: str):
    normalized = algorithm.strip().lower()
    if normalized == "md5":
        return hashlib.md5
    if normalized == "sha256":
        return hashlib.sha256
    if normalized == "sha512":
        return hashlib.sha512
    raise RobokassaConfigError(f"Unsupported Robokassa hash algorithm: {algorithm}")


def _out_sum_from_cents(amount_cents: int) -> str:
    return f"{amount_cents / 100:.2f}"


def _required(value: str | None, env_name: str) -> str:
    if not value:
        raise RobokassaConfigError(f"{env_name} is not set")
    return value


def _password1(config: Config) -> str | None:
    if config.robokassa_test_mode:
        return config.robokassa_test_password1 or config.robokassa_password1
    return config.robokassa_password1


def _password2(config: Config) -> str | None:
    if config.robokassa_test_mode:
        return config.robokassa_test_password2 or config.robokassa_password2
    return config.robokassa_password2

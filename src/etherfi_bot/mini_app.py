from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route
from telegram import Update

from etherfi_bot.domain import ManualTopUpError


MAX_BODY_BYTES = 16_384
MAX_INIT_DATA_AGE_SECONDS = 300
STATIC_DIR = Path(__file__).with_name("static") / "mini_app"


class InitDataError(ValueError):
    pass


def validate_init_data(
    raw: str,
    bot_token: str,
    *,
    now: int | None = None,
    max_age_seconds: int = MAX_INIT_DATA_AGE_SECONDS,
) -> int:
    if not raw or len(raw) > 8192:
        raise InitDataError("Invalid Telegram authorization")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise InitDataError("Invalid Telegram authorization") from error
    data: dict[str, str] = {}
    for key, value in pairs:
        if key in data:
            raise InitDataError("Duplicate Telegram authorization field")
        data[key] = value
    received_hash = data.pop("hash", None)
    if not received_hash:
        raise InitDataError("Missing Telegram authorization hash")
    check_string = "\n".join(f"{key}={data[key]}" for key in sorted(data))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise InitDataError("Invalid Telegram authorization signature")
    try:
        auth_date = int(data["auth_date"])
        user = json.loads(data["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise InitDataError("Invalid Telegram authorization payload") from error
    current = int(time.time() if now is None else now)
    if auth_date > current + 30 or current - auth_date > max_age_seconds:
        raise InitDataError("Telegram authorization has expired")
    return user_id


def create_mini_app(
    *,
    application: object,
    dispatcher: object,
    bot_token: str,
    webhook_path: str,
    webhook_secret_token: str,
    mini_app_public_url: str,
) -> Starlette:
    mini_path = urlsplit(mini_app_public_url).path.rstrip("/")

    async def page(_request: Request) -> Response:
        return _file("index.html", "text/html")

    async def css(_request: Request) -> Response:
        return _file("app.css", "text/css")

    async def javascript(_request: Request) -> Response:
        return _file("app.js", "application/javascript")

    async def context(request: Request) -> Response:
        try:
            user_id = _authorize(request, bot_token)
            value = await dispatcher.manual_top_up_context(user_id)
            if value is None:
                raise ManualTopUpError("Manual top-up is not available")
            return _json({
                "target_balance": str(value.target_balance),
                "safe_balance": str(value.safe_balance),
                "maximum_amount": str(value.maximum_amount),
                "preset_amounts": [str(item) for item in value.preset_amounts],
                "target_account": value.target_account,
                "safe_account": value.safe_account,
            })
        except (InitDataError, ManualTopUpError) as error:
            return _error(error)

    async def prepare(request: Request) -> Response:
        try:
            user_id = _authorize(request, bot_token)
            body = await _json_body(request)
            amount = Decimal(str(body.get("amount", "")))
            state = await dispatcher.prepare_manual_top_up(user_id, amount)
            if state is None:
                raise ManualTopUpError("Manual top-up is not available")
            return _json({"ok": True})
        except (InitDataError, ManualTopUpError, InvalidOperation, ValueError) as error:
            return _error(error)

    async def webhook(request: Request) -> Response:
        secret = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not hmac.compare_digest(secret, webhook_secret_token):
            return Response(status_code=403)
        try:
            payload = await _json_body(request)
            update = Update.de_json(payload, application.bot)
            await application.update_queue.put(update)
        except (ValueError, json.JSONDecodeError):
            return Response(status_code=400)
        return Response(status_code=200)

    routes = [
        Route(mini_path, page),
        Route(f"{mini_path}/", page),
        Route(f"{mini_path}/app.css", css),
        Route(f"{mini_path}/app.js", javascript),
        Route(f"{mini_path}/api/context", context, methods=["GET"]),
        Route(f"{mini_path}/api/prepare", prepare, methods=["POST"]),
        Route(webhook_path, webhook, methods=["POST"]),
    ]
    return Starlette(routes=routes)


def _authorize(request: Request, bot_token: str) -> int:
    scheme, _, raw = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "tma":
        raise InitDataError("Missing Telegram authorization")
    return validate_init_data(raw, bot_token)


async def _json_body(request: Request) -> dict[str, object]:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("Request is too large")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def _file(name: str, media_type: str) -> FileResponse:
    return FileResponse(
        STATIC_DIR / name,
        media_type=media_type,
        headers=_security_headers(),
    )


def _json(value: dict[str, object], status_code: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status_code, headers=_security_headers())


def _error(error: Exception) -> JSONResponse:
    status = 401 if isinstance(error, InitDataError) else 400
    return _json({"error": str(error)}, status)


def _security_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self' https://telegram.org; "
            "style-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "frame-ancestors https://web.telegram.org https://*.telegram.org"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }

#!/usr/bin/env python3
"""Create an operator-only Robokassa test order and emit a local POST form."""

from __future__ import annotations

import argparse
import html
import http.cookiejar
import json
import secrets
import time
import urllib.request
from pathlib import Path
from typing import Any

_ACTION = "https://auth.robokassa.ru/Merchant/Index.aspx"


def _json_request(
    url: str, *, body: dict[str, str] | None = None
) -> urllib.request.Request:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Idempotency-Key"] = secrets.token_urlsafe(32)
    return urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data else "GET"
    )


def _read_json(
    opener: urllib.request.OpenerDirector, request: urllib.request.Request
) -> dict[str, Any]:
    with opener.open(request, timeout=15) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError("unexpected API response")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a FetchNow Robokassa test order; no secrets are read."
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=0,
        help="poll safe order status with the in-memory anonymous cookie",
    )
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    _read_json(opener, _json_request(f"{base}/api/v1/media/quota"))
    order = _read_json(
        opener,
        _json_request(
            f"{base}/api/v1/payments/orders", body={"productCode": "premium_24h"}
        ),
    )
    form = order.get("paymentForm")
    if not isinstance(form, dict) or form.get("action") != _ACTION:
        raise RuntimeError("API returned an unexpected payment action")
    fields = form.get("fields")
    if not isinstance(fields, dict) or fields.get("IsTest") != "1":
        raise RuntimeError("API did not return a fail-closed test form")
    controls = "\n".join(
        f'<input type="hidden" name="{html.escape(str(key), quote=True)}" '
        f'value="{html.escape(str(value), quote=True)}">'
        for key, value in fields.items()
    )
    document = (
        '<!doctype html><meta charset="utf-8"><title>FetchNow test payment</title>'
        f'<form action="{_ACTION}" method="post">{controls}'
        '<button type="submit">Open Robokassa test payment</button></form>'
    )
    args.output.write_text(document, encoding="utf-8")
    args.output.chmod(0o600)
    print(f"orderId={order.get('orderId')}")
    print(f"status={order.get('status')}")
    print(f"form={args.output}")
    order_id = order.get("orderId")
    if args.wait_seconds < 0 or args.wait_seconds > 900:
        raise RuntimeError("--wait-seconds must be between 0 and 900")
    if args.wait_seconds and isinstance(order_id, str):
        deadline = time.monotonic() + args.wait_seconds
        last_status: object = order.get("status")
        while time.monotonic() < deadline:
            status = _read_json(
                opener,
                _json_request(f"{base}/api/v1/payments/orders/{order_id}"),
            )
            current = status.get("status")
            if current != last_status:
                print(f"status={current}")
                last_status = current
            if current in {"PAID", "EXPIRED"}:
                break
            time.sleep(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

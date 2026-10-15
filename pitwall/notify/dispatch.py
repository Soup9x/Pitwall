"""The single notify() path. Every channel (email and all webhook types) goes through Apprise.

Generic JSON webhooks (json:// and jsons://) are routed through a small custom Apprise plugin so
the POSTed body is Pitwall's own payload: {"type": ..., "title": ..., "text": ..., "data": ...}.
Every other Apprise service (discord://, ntfy://, slack://, ...) receives the plain-text body."""

from __future__ import annotations

import json
import logging
from typing import Any

import apprise
import httpx
from apprise.decorators import notify as apprise_plugin

from pitwall import USER_AGENT
from pitwall.config import get_env, split_urls
from pitwall.notify.message import Message

log = logging.getLogger(__name__)

_JSON_SCHEMES = {"json": "pwjson", "jsons": "pwjsons"}


def _post_json(body: str, meta: dict[str, Any], secure: bool) -> bool:
    scheme = "https" if secure else "http"
    host = meta.get("host", "")
    port = meta.get("port")
    url = f"{scheme}://{host}" + (f":{port}" if port else "") + (meta.get("fullpath") or "/")
    auth = (meta["user"], meta.get("password") or "") if meta.get("user") else None
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    for key, value in (meta.get("qsd+") or {}).items():
        headers[key] = value
    try:
        resp = httpx.post(url, content=body.encode(), headers=headers, auth=auth, timeout=15)
    except httpx.HTTPError as exc:
        log.warning("webhook POST failed", extra={"error": type(exc).__name__})
        return False
    if resp.status_code >= 400:
        log.warning("webhook POST rejected", extra={"status": resp.status_code})
        return False
    return True


@apprise_plugin(on="pwjson", name="Pitwall JSON webhook")
def _pwjson(
    body: str, title: str, notify_type: str, meta: dict[str, Any], *args: Any, **kwargs: Any
) -> bool:
    return _post_json(body, meta, secure=False)


@apprise_plugin(on="pwjsons", name="Pitwall JSON webhook (https)")
def _pwjsons(
    body: str, title: str, notify_type: str, meta: dict[str, Any], *args: Any, **kwargs: Any
) -> bool:
    return _post_json(body, meta, secure=True)


def channel_urls(channel: str) -> list[str]:
    env = get_env()
    blob = env.apprise_urls_email if channel == "email" else env.apprise_urls_webhook
    return split_urls(blob)


def mask_url(url: str) -> str:
    """Show only the scheme and host, e.g. 'mailtos://smtp.gmail.com/…'."""
    scheme, _, rest = url.partition("://")
    host = rest.split("?")[0].split("/")[0].rsplit("@", 1)[-1]
    if len(host) > 24:
        host = host[:10] + "…" + host[-6:]
    return f"{scheme}://{host}/…" if rest else f"{scheme}://…"


def _rewrite(url: str) -> tuple[str, bool]:
    scheme, sep, rest = url.partition("://")
    if sep and scheme.lower() in _JSON_SCHEMES:
        return f"{_JSON_SCHEMES[scheme.lower()]}://{rest}", True
    return url, False


def notify(channel: str, msg: Message, urls: list[str] | None = None) -> tuple[bool, str]:
    """Deliver `msg` on `channel` ('email' or 'webhook'). Returns (ok, detail)."""
    urls = channel_urls(channel) if urls is None else urls
    if not urls:
        return False, f"no Apprise URLs configured for {channel}"

    plain = apprise.Apprise()
    custom = apprise.Apprise()
    for url in urls:
        rewritten, is_json = _rewrite(url)
        target = custom if is_json else plain
        if not target.add(rewritten):
            return False, f"invalid Apprise URL ({mask_url(url)})"

    ok = True
    with apprise.LogCapture(level=logging.WARNING) as captured:
        if len(plain):
            if channel == "email" and msg.html:
                ok &= bool(
                    plain.notify(
                        body=msg.html, title=msg.title, body_format=apprise.NotifyFormat.HTML
                    )
                )
            else:
                ok &= bool(
                    plain.notify(
                        body=msg.text, title=msg.title, body_format=apprise.NotifyFormat.TEXT
                    )
                )
        if len(custom):
            ok &= bool(custom.notify(body=json.dumps(msg.webhook_payload()), title=msg.title))
        detail = captured.getvalue().strip()
    return ok, (detail[-500:] if detail else ("delivered" if ok else "delivery failed"))

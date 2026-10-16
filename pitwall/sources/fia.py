"""FIA event documents: the season's documents page (HTML) and the PDFs it links to.

The page address isn't predictable (it ends in an FIA-internal id), so an admin pastes it in
Penalty watch → Settings. Pitwall only reads the page and PDFs; it never guesses a document's
event: a PDF is matched to an event only when its address or title names that Grand Prix."""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote, urljoin
from zoneinfo import ZoneInfo

import httpx

from pitwall import USER_AGENT
from pitwall.sources.http import RateLimiter, SourceUnavailable

log = logging.getLogger(__name__)

_limiter = RateLimiter(min_interval=3.0, per_hour=300)
_PARIS = ZoneInfo("Europe/Paris")  # the FIA site states times in CET/CEST

_LINK = re.compile(r"<a\b[^>]*href=\"([^\"]+\.pdf)\"[^>]*>(.*?)</a>", re.I | re.S)
_PUBLISHED = re.compile(
    r"Published on\s*(\d{2})\.(\d{2})\.(\d{2})\s+(\d{2}):(\d{2})(?:\s*(CEST|CET))?", re.I
)
_OFFSETS = {"CET": timezone(timedelta(hours=1)), "CEST": timezone(timedelta(hours=2))}
_TAGS = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class DocRef:
    title: str
    url: str
    published_at: datetime | None


def parse_listing(page: str, base_url: str) -> list[DocRef]:
    """PDF links on a documents page, with the "Published on" time that follows each link."""
    out: list[DocRef] = []
    seen: set[str] = set()
    for m in _LINK.finditer(page):
        url = urljoin(base_url, html.unescape(m.group(1)))
        if url in seen:
            continue
        seen.add(url)
        title = " ".join(html.unescape(_TAGS.sub(" ", m.group(2))).split())
        if not title:
            title = unquote(url.rsplit("/", 1)[-1]).removesuffix(".pdf")
        published = None
        p = _PUBLISHED.search(page, m.end(), m.end() + 600)
        if p:
            dd, mm, yy, hh, mi = (int(x) for x in p.groups()[:5])
            tz = _OFFSETS.get((p.group(6) or "").upper(), _PARIS)
            try:
                published = datetime(2000 + yy, mm, dd, hh, mi, tzinfo=tz).astimezone(UTC)
            except ValueError:
                published = None
        out.append(DocRef(title[:300], url, published))
    return out


def names_event(ref: DocRef, event_name: str, season: int) -> bool:
    """Whether the document's address or title names this event (and season)."""
    blob = f"{unquote(ref.url)} {ref.title}".lower()
    return event_name.lower() in blob and str(season) in blob


class FiaClient:
    def __init__(
        self,
        *,
        limiter: RateLimiter | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.limiter = limiter or _limiter
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _get(self, url: str) -> httpx.Response:
        self.limiter.acquire()
        try:
            resp = self._client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"fia: {exc}") from exc
        return resp

    def listing(self, page_url: str) -> list[DocRef]:
        return parse_listing(self._get(page_url).text, page_url)

    def pdf(self, url: str, max_bytes: int = 15_000_000) -> bytes:
        body = self._get(url).content
        if len(body) > max_bytes:
            raise SourceUnavailable(f"fia: {url} is larger than {max_bytes} bytes")
        return body


def make_client(**kwargs: Any) -> FiaClient:
    return FiaClient(**kwargs)

"""Notification message model and dedup hashing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pitwall.models import OWNER_ID


@dataclass
class Message:
    type: str  # alert type, see settings_store.ALERT_TYPES
    title: str
    text: str
    html: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    event_id: int | None = None
    session_id: int | None = None
    url: str | None = None
    # Drop instead of delivering late, e.g. a reminder whose session has already started.
    expires_at: datetime | None = None
    # Content key used for dedup; defaults to the title + text.
    dedup_content: str | None = None
    # Recipient. None means the owner account (single-user installs).
    user_id: int | None = None

    def payload_hash(self) -> str:
        fields: dict[str, Any] = {}
        if self.user_id not in (None, OWNER_ID):
            # The owner's fingerprints stay as before multi-user support, so nothing already
            # sent is re-sent after upgrading.
            fields["user"] = self.user_id
        blob = json.dumps(
            {
                **fields,
                "type": self.type,
                "event": self.event_id,
                "session": self.session_id,
                "content": self.dedup_content
                if self.dedup_content is not None
                else f"{self.title}\n{self.text}",
            },
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode()).hexdigest()

    def webhook_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type, "title": self.title, "text": self.text}
        if self.url:
            payload["url"] = self.url
        if self.data:
            payload["data"] = self.data
        return payload

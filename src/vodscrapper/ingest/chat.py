"""VOD chat retrieval.

Chat is the one input the local recording cannot provide, so it is the only
reason the pipeline touches the VOD at all.

Two sources are supported:

1. A JSON file exported by TwitchDownloaderCLI -- preferred when available,
   since it is a maintained tool and insulates us from Twitch's internals.
2. Twitch's private GraphQL endpoint, used directly.

Source 2 is **undocumented and unsupported by Twitch**. It is what every
chat downloader uses, and it works, but it can break without notice. When it
does, the fix is to fall back to source 1 rather than to assume the stream
had no chat -- so a failure here raises rather than silently returning an
empty list.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import requests

from ..models import ChatMessage
from ..timing import vod_offset_to_recording_offset

GQL_URL = "https://gql.twitch.tv/gql"
# Twitch's public web client id. Not a secret -- it ships in the website.
GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
_PERSISTED_QUERY = {
    "version": 1,
    "sha256Hash": "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a",
}


class ChatError(RuntimeError):
    pass


def _iter_gql_pages(video_id: str, timeout: float = 20.0) -> Iterator[dict[str, Any]]:
    session = requests.Session()
    cursor: str | None = None
    offset = 0
    while True:
        variables: dict[str, Any] = {"videoID": str(video_id)}
        if cursor:
            variables["cursor"] = cursor
        else:
            variables["contentOffsetSeconds"] = offset

        payload = [{
            "operationName": "VideoCommentsByOffsetOrCursor",
            "variables": variables,
            "extensions": {"persistedQuery": _PERSISTED_QUERY},
        }]
        resp = session.post(
            GQL_URL,
            json=payload,
            headers={"Client-ID": GQL_CLIENT_ID},
            timeout=timeout,
        )
        if resp.status_code >= 400:
            raise ChatError(
                f"Twitch GQL returned {resp.status_code}. This endpoint is "
                "undocumented and may have changed; export chat with "
                "TwitchDownloaderCLI and pass --chat-json instead."
            )
        body = resp.json()
        if not body or not isinstance(body, list):
            raise ChatError("unexpected GQL response shape")
        comments = (body[0].get("data") or {}).get("video", {})
        if comments is None:
            raise ChatError(f"no chat available for video {video_id}")
        comments = comments.get("comments") or {}
        edges = comments.get("edges") or []
        if not edges:
            return
        for edge in edges:
            yield edge
        if not comments.get("pageInfo", {}).get("hasNextPage"):
            return
        cursor = edges[-1].get("cursor")
        if not cursor:
            return


def _node_to_message(node: dict[str, Any], offset_shift: float) -> ChatMessage | None:
    try:
        vod_offset = float(node.get("contentOffsetSeconds", 0.0))
    except (TypeError, ValueError):
        return None
    commenter = node.get("commenter") or {}
    message = node.get("message") or {}
    fragments = message.get("fragments") or []

    text_parts: list[str] = []
    emotes: list[str] = []
    for fragment in fragments:
        text = fragment.get("text") or ""
        text_parts.append(text)
        if fragment.get("emote"):
            # Emote fragments carry their name in the text field.
            emotes.append(text.strip())

    return ChatMessage(
        offset=vod_offset + offset_shift,
        user=commenter.get("displayName") or commenter.get("login") or "",
        text="".join(text_parts).strip(),
        emotes=[e for e in emotes if e],
    )


def fetch_chat(
    video_id: str,
    vod_start_utc: datetime,
    recording_start_utc: datetime,
) -> list[ChatMessage]:
    """Download VOD chat and convert it onto the recording timeline."""
    shift = vod_offset_to_recording_offset(0.0, vod_start_utc, recording_start_utc)
    messages: list[ChatMessage] = []
    for edge in _iter_gql_pages(video_id):
        node = edge.get("node") or {}
        msg = _node_to_message(node, shift)
        if msg is not None:
            messages.append(msg)
    messages.sort(key=lambda m: m.offset)
    return messages


def load_chat_json(
    path: str | Path,
    vod_start_utc: datetime,
    recording_start_utc: datetime,
) -> list[ChatMessage]:
    """Load a TwitchDownloaderCLI chat export."""
    shift = vod_offset_to_recording_offset(0.0, vod_start_utc, recording_start_utc)
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    comments = raw.get("comments") if isinstance(raw, dict) else raw
    if not isinstance(comments, list):
        raise ChatError(f"unrecognised chat export format: {path}")

    messages: list[ChatMessage] = []
    for comment in comments:
        offset = comment.get("content_offset_seconds")
        if offset is None:
            continue
        body = comment.get("message") or {}
        text = body.get("body", "") if isinstance(body, dict) else str(body)
        emotes = []
        for fragment in (body.get("fragments") or []) if isinstance(body, dict) else []:
            if fragment.get("emoticon"):
                emotes.append((fragment.get("text") or "").strip())
        commenter = comment.get("commenter") or {}
        messages.append(
            ChatMessage(
                offset=float(offset) + shift,
                user=commenter.get("display_name") or commenter.get("name") or "",
                text=text.strip(),
                emotes=[e for e in emotes if e],
            )
        )
    messages.sort(key=lambda m: m.offset)
    return messages


def save_chat(messages: list[ChatMessage], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            [
                {"offset": m.offset, "user": m.user, "text": m.text, "emotes": m.emotes}
                for m in messages
            ],
            fh,
        )


def load_cached_chat(path: str | Path) -> list[ChatMessage]:
    with open(path, "r", encoding="utf-8") as fh:
        return [ChatMessage(**entry) for entry in json.load(fh)]

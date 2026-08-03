"""Twitch Helix client.

Only three things are needed from Twitch: which VOD corresponds to a local
recording, that VOD's start time (to anchor chat), and native clip creation.
Everything else comes from the local recording, which is both higher quality
and not subject to VOD expiry.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

HELIX = "https://api.twitch.tv/helix"


class TwitchError(RuntimeError):
    pass


@dataclass
class Vod:
    id: str
    title: str
    created_at: datetime
    published_at: datetime
    duration_seconds: float
    url: str

    @property
    def end_at(self) -> datetime:
        return self.created_at + timedelta(seconds=self.duration_seconds)


def parse_duration(text: str) -> float:
    """Parse Twitch's '3h20m14s' duration format into seconds."""
    total = 0.0
    number = ""
    for ch in text:
        if ch.isdigit():
            number += ch
            continue
        if not number:
            continue
        value = int(number)
        number = ""
        if ch == "h":
            total += value * 3600
        elif ch == "m":
            total += value * 60
        elif ch == "s":
            total += value
    return total


def _parse_vod(raw: dict[str, Any]) -> Vod:
    return Vod(
        id=raw["id"],
        title=raw.get("title", ""),
        created_at=datetime.fromisoformat(raw["created_at"].replace("Z", "+00:00")),
        published_at=datetime.fromisoformat(
            raw.get("published_at", raw["created_at"]).replace("Z", "+00:00")
        ),
        duration_seconds=parse_duration(raw.get("duration", "0s")),
        url=raw.get("url", ""),
    )


class TwitchClient:
    def __init__(self, client_id: str, oauth_token: str, timeout: float = 20.0):
        if not client_id:
            raise TwitchError("twitch.client_id is not configured")
        if not oauth_token:
            raise TwitchError(
                "no OAuth token found; set the env var named by twitch.oauth_token_env. "
                "The token needs the clips:edit scope to create clips."
            )
        self.client_id = client_id
        self.token = oauth_token.removeprefix("oauth:")
        self.timeout = timeout
        self._session = requests.Session()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Client-Id": self.client_id,
            "Authorization": f"Bearer {self.token}",
        }

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        url = f"{HELIX}/{path.lstrip('/')}"
        for attempt in range(4):
            resp = self._session.request(
                method, url, headers=self._headers, timeout=self.timeout, **kwargs
            )
            if resp.status_code == 429:
                # Helix uses a token bucket and tells us when it refills.
                reset = resp.headers.get("Ratelimit-Reset")
                wait = 2.0 * (attempt + 1)
                if reset:
                    wait = max(wait, float(reset) - time.time() + 0.5)
                time.sleep(min(wait, 30.0))
                continue
            if resp.status_code == 401:
                raise TwitchError(
                    "Twitch rejected the token (401). It may be expired, or missing "
                    "the clips:edit scope."
                )
            if resp.status_code >= 400:
                raise TwitchError(f"{method} {path} -> {resp.status_code}: {resp.text[:400]}")
            return resp.json() if resp.content else {}
        raise TwitchError(f"{method} {path} rate limited after retries")

    # -- lookups -----------------------------------------------------------

    def user_id(self, login: str) -> str:
        data = self._request("GET", "users", params={"login": login})
        entries = data.get("data", [])
        if not entries:
            raise TwitchError(f"no such Twitch user: {login}")
        return entries[0]["id"]

    def videos(self, user_id: str, first: int = 20) -> list[Vod]:
        data = self._request(
            "GET", "videos",
            params={"user_id": user_id, "first": min(first, 100), "type": "archive"},
        )
        return [_parse_vod(raw) for raw in data.get("data", [])]

    def video(self, video_id: str) -> Vod | None:
        """Look up a single VOD by id.

        Needed by ``vodscrap clip``: a session records which VOD it matched,
        but native clip creation also needs that VOD's start time to convert
        recording offsets into VOD offsets, and its length to reject
        candidates that fall outside it.
        """
        data = self._request("GET", "videos", params={"id": video_id})
        entries = data.get("data", [])
        return _parse_vod(entries[0]) if entries else None

    def validate_token(self) -> dict[str, Any]:
        """Ask Twitch what this token actually is.

        The validate endpoint lives on id.twitch.tv rather than Helix, and it
        is the only way to see a token's granted scopes -- which is what
        turns "clip creation failed" into "your token is missing clips:edit".
        """
        resp = self._session.get(
            "https://id.twitch.tv/oauth2/validate",
            headers={"Authorization": f"OAuth {self.token}"},
            timeout=self.timeout,
        )
        if resp.status_code == 401:
            raise TwitchError("token is expired or invalid (401 from oauth2/validate)")
        if resp.status_code >= 400:
            raise TwitchError(f"oauth2/validate -> {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def match_vod(
        self,
        user_id: str,
        recording_start_utc: datetime,
        tolerance_minutes: float = 45.0,
    ) -> Vod | None:
        """Find the VOD that overlaps a local recording.

        Recording and stream rarely start at the same instant -- Nick may hit
        record before going live, or vice versa -- so this matches on overlap
        with a generous tolerance rather than on exact equality.
        """
        recording_start_utc = recording_start_utc.astimezone(timezone.utc)
        best: tuple[float, Vod] | None = None
        for vod in self.videos(user_id, first=40):
            delta = abs((vod.created_at - recording_start_utc).total_seconds())
            if delta > tolerance_minutes * 60:
                continue
            if best is None or delta < best[0]:
                best = (delta, vod)
        return best[1] if best else None

    # -- clip creation -----------------------------------------------------

    def create_clip_from_vod(
        self,
        broadcaster_id: str,
        video_id: str,
        start_offset: float,
        duration: float,
        vod_offset_is_start: bool = True,
    ) -> dict[str, Any]:
        """Create a native Twitch clip at a VOD position.

        ``start_offset`` is always where Nick wants the clip to BEGIN. The
        ``vod_offset_is_start`` flag exists because the documented meaning of
        the API's own ``vod_offset`` parameter is contested: Twitch's
        reference calls it the start, multiple client libraries treat it as
        the end. Getting it wrong shifts every clip by its own length, so the
        translation happens here in one place and is settled empirically by
        ``vodscrap verify-clip-offset``.
        """
        duration = max(5.0, min(60.0, duration))
        offset = start_offset if vod_offset_is_start else start_offset + duration
        data = self._request(
            "POST", "clips",
            params={
                "broadcaster_id": broadcaster_id,
                "video_id": video_id,
                "vod_offset": int(round(offset)),
                "duration": int(round(duration)),
            },
        )
        entries = data.get("data", [])
        return entries[0] if entries else {}

    def get_clip(self, clip_id: str) -> dict[str, Any]:
        data = self._request("GET", "clips", params={"id": clip_id})
        entries = data.get("data", [])
        return entries[0] if entries else {}

    def clip_vod_offset(self, clip_id: str) -> float | None:
        """Read back the clip's own recorded vod_offset.

        This is what makes the semantics test possible: create a clip with a
        known intended start, then ask Twitch where it thinks the clip sits.
        """
        clip = self.get_clip(clip_id)
        value = clip.get("vod_offset")
        return float(value) if value is not None else None

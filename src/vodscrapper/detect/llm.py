"""LLM reranking, titling, and bound-tightening.

This runs last and only on candidates that survived the cheap detectors, so
the model reads a few dozen short transcript windows per session rather than
eight hours of speech.

Two things it is deliberately NOT allowed to do:

* It cannot drop a marker. Nick pressed the key on purpose; a model's opinion
  does not override that. Marker candidates are scored for ordering and
  titling only.
* It cannot widen a clip beyond the window it was given. Tightening inward is
  useful; letting it wander produces clips whose bounds nobody chose.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from ..config import LLMDetect
from ..models import Candidate, Source
from ..timing import clamp
from .transcript import Transcription

_SYSTEM_HEAD = """You judge whether a moment from a Twitch stream is worth \
posting as a short-form clip (YouTube Shorts, TikTok)."""

_SYSTEM_TAIL = """You are given a transcript window and the signals that \
flagged it. Judge only what the transcript and signals support -- do not \
invent events you cannot see. A moment is worth posting when something \
actually happens: a big play, a genuinely funny exchange, a dramatic loss, a \
story worth hearing. Ordinary narration, quiet menu talk, and downtime are \
not.

Return only the JSON object the schema describes."""

# No game is named here. The streamer plays several, and a prompt that
# confidently describes the wrong one is worse than a prompt that describes
# none -- it invites the model to read football commentary as an extraction
# run. The active game's description is injected by the caller, and when
# there isn't one the prompt simply stays generic.
_SYSTEM_GENERIC_GAME = """The streamer plays a variety of games. Infer what \
is happening from the transcript rather than assuming a genre."""


def build_system_prompt(game_description: str = "", streams_with_group: bool = True) -> str:
    """Assemble the reranker's system prompt for the active game."""
    middle = (game_description or "").strip() or _SYSTEM_GENERIC_GAME
    if streams_with_group:
        middle += " He streams solo and with a group."
    return f"{_SYSTEM_HEAD}\n\n{middle}\n\n{_SYSTEM_TAIL}"


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {
            "type": "integer",
            "description": "0-100. How likely this is worth posting.",
        },
        "verdict": {"type": "string", "enum": ["post", "maybe", "skip"]},
        "reason": {
            "type": "string",
            "description": "One sentence, grounded in the transcript.",
        },
        "title": {
            "type": "string",
            "description": "Short-form title, under 70 characters, no clickbait padding.",
        },
        "description": {"type": "string"},
        "hashtags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3-6 tags, no leading '#'.",
        },
        "start_offset": {
            "type": "number",
            "description": (
                "Seconds from the START of the supplied window where the clip "
                "should begin. Must be >= 0."
            ),
        },
        "end_offset": {
            "type": "number",
            "description": (
                "Seconds from the START of the supplied window where the clip "
                "should end. Must not exceed the window length."
            ),
        },
    },
    "required": [
        "score", "verdict", "reason", "title",
        "description", "hashtags", "start_offset", "end_offset",
    ],
    "additionalProperties": False,
}


@dataclass
class Judgement:
    score: float
    verdict: str
    reason: str
    title: str
    description: str
    hashtags: list[str]
    start_offset: float
    end_offset: float


class Reranker:
    def __init__(self, cfg: LLMDetect, game_description: str = ""):
        self.cfg = cfg
        self.system = build_system_prompt(game_description)
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "LLM reranking needs the anthropic SDK: "
                "pip install 'vodscrapper[analyze]'"
            ) from exc
        # A bare constructor also picks up an `ant auth login` profile, so an
        # unset API key is not necessarily an error.
        key = os.environ.get(self.cfg.api_key_env)
        self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        return self._client

    def _build_prompt(self, candidate: Candidate, transcript: str, window: float) -> str:
        signals = []
        ev = candidate.evidence
        if Source.MARKER in candidate.sources:
            signals.append(
                f"The streamer pressed his marker hotkey here "
                f"(category: {candidate.category.value}). This is a deliberate flag."
            )
        if "chat_z" in ev:
            signals.append(
                f"Chat spiked to {ev['chat_z']} standard deviations above its "
                f"rolling baseline: {ev.get('messages', 0)} messages, "
                f"{ev.get('laugh_emotes', 0)} laugh emotes, "
                f"{ev.get('hype_emotes', 0)} hype emotes, "
                f"{ev.get('clip_calls', 0)} viewers asking for a clip."
            )
        if "peak_db" in ev:
            silence = " following a quiet stretch" if ev.get("from_silence") else ""
            signals.append(
                f"His microphone jumped {ev.get('above_baseline_db')} dB above "
                f"baseline for {ev.get('sustained_seconds')}s{silence}."
            )
        if "kills" in ev or "survived" in ev:
            outcome = "extracted" if ev.get("survived") else "died"
            signals.append(
                f"The match that follows ended with {ev.get('kills')} kills and he {outcome}."
            )

        return (
            f"Signals that flagged this moment:\n"
            + "\n".join(f"- {s}" for s in signals)
            + f"\n\nWindow length: {window:.1f} seconds.\n"
            f"Transcript of the window:\n\"\"\"\n{transcript or '(no speech detected)'}\n\"\"\""
        )

    def judge(
        self,
        candidate: Candidate,
        transcription: Transcription | None,
    ) -> Judgement | None:
        client = self._get_client()
        if transcription is not None:
            window_start = transcription.window_start
            window_end = transcription.window_end
            text = transcription.text
        else:
            window_start, window_end = candidate.start, candidate.end
            text = candidate.transcript
        window = max(1.0, window_end - window_start)

        request: dict[str, Any] = {
            "model": self.cfg.model,
            "max_tokens": 4096,
            "system": self.system,
            "messages": [
                {"role": "user", "content": self._build_prompt(candidate, text, window)}
            ],
            "output_config": {
                "format": {"type": "json_schema", "schema": _SCHEMA},
                # Judging a 90-second transcript snippet is not hard reasoning,
                # and this runs once per candidate -- low effort keeps a long
                # session's rerank pass cheap without hurting the verdicts.
                "effort": "low",
            },
        }

        try:
            response = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                **request,
            )
        except Exception:
            # Server-side fallback is Claude-API-only and beta; if it is
            # unavailable, the judgement itself still works without it.
            response = client.messages.create(**request)

        # A refused request returns HTTP 200 with empty or partial content, so
        # this has to be checked before touching content[0].
        if getattr(response, "stop_reason", None) == "refusal":
            print("[llm] request was declined; leaving candidate unscored")
            return None

        text_block = next((b for b in response.content if b.type == "text"), None)
        if text_block is None:
            return None
        try:
            raw = json.loads(text_block.text)
        except json.JSONDecodeError:
            return None

        start = max(0.0, float(raw.get("start_offset", 0.0)))
        end = min(window, float(raw.get("end_offset", window)))
        if end <= start:
            start, end = 0.0, window

        return Judgement(
            score=float(raw.get("score", 0)),
            verdict=str(raw.get("verdict", "maybe")),
            reason=str(raw.get("reason", "")),
            title=str(raw.get("title", "")),
            description=str(raw.get("description", "")),
            hashtags=[str(h).lstrip("#") for h in raw.get("hashtags", [])],
            start_offset=start + window_start,
            end_offset=end + window_start,
        )


def rerank(
    candidates: list[Candidate],
    transcriptions: dict[int, Transcription],
    duration: float,
    cfg: LLMDetect,
    tighten: bool = True,
    game_description: str = "",
) -> None:
    """Score, title, and optionally tighten candidates in place."""
    if not cfg.enabled or not candidates:
        return

    reranker = Reranker(cfg, game_description=game_description)
    for idx, candidate in enumerate(candidates):
        try:
            judgement = reranker.judge(candidate, transcriptions.get(idx))
        except Exception as exc:  # pragma: no cover - network dependent
            print(f"[llm] candidate {idx} failed: {exc}")
            continue
        if judgement is None:
            continue

        contribution = (judgement.score / 100.0) * cfg.max_score
        candidate.score += contribution
        candidate.evidence["llm_score"] = judgement.score
        candidate.evidence["llm_verdict"] = judgement.verdict
        candidate.evidence["llm_reason"] = judgement.reason

        if cfg.generate_titles:
            candidate.title = judgement.title
            candidate.description = judgement.description
            candidate.hashtags = judgement.hashtags

        # Tightening is inward-only, and never applied to a marker: Nick chose
        # that moment, so the model may reorder it but not re-cut it.
        if tighten and cfg.tighten_bounds and Source.MARKER not in candidate.sources:
            new_start = clamp(judgement.start_offset, candidate.start, candidate.end)
            new_end = clamp(judgement.end_offset, candidate.start, candidate.end)
            if new_end - new_start >= 3.0:
                candidate.start = clamp(new_start, 0.0, duration)
                candidate.end = clamp(new_end, 0.0, duration)

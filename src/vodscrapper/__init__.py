"""Turn a Twitch stream into clips worth posting.

The pipeline anchors everything to the local recording's start time, so a
hotkey press during the stream maps to a frame offset by plain subtraction
rather than by guessing at VOD drift. See docs/architecture.md.
"""

__version__ = "0.1.0"

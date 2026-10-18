"""Keep one useful wake decode after the active one, never an old duplicate."""
from __future__ import annotations

from typing import Any


def prefer_recording(incoming: Any, current: Any) -> bool:
    """A new utterance may be shorter than an older, already rejected phrase."""
    new_at = float(getattr(incoming, "captured_at", 0.0) or 0.0)
    old_at = float(getattr(current, "captured_at", 0.0) or 0.0)
    if new_at and old_at and new_at != old_at:
        return new_at > old_at
    new_id = str(getattr(incoming, "correlation_id", "") or "")
    old_id = str(getattr(current, "correlation_id", "") or "")
    if new_id and old_id and new_id != old_id:
        return True
    return incoming.audio.size / max(1, incoming.sample_rate) >= current.audio.size / max(1, current.sample_rate)


def select_pending(generation: int, complete: tuple | None, probe: tuple | None) -> tuple[str, tuple] | None:
    """Completed PCM supersedes its partial probe; another newer utterance wins.

    Both mailbox slots are consumed by the caller.  Retaining the losing partial
    used to decode the same speech again after its completed decode had failed,
    delaying a newly spoken wake word and spending native inference time twice.
    """
    complete = complete if complete is not None and complete[1] == generation else None
    probe = probe if probe is not None and probe[1] == generation else None
    if complete is None:
        return ("probe", probe) if probe is not None else None
    if probe is None:
        return "complete", complete
    complete_id = str(getattr(complete[0], "correlation_id", "") or "")
    probe_id = str(getattr(probe[0], "correlation_id", "") or "")
    if complete_id and complete_id == probe_id:
        return "complete", complete
    complete_at = float(getattr(complete[0], "captured_at", 0.0) or 0.0)
    probe_at = float(getattr(probe[0], "captured_at", 0.0) or 0.0)
    if complete_at and probe_at and probe_at > complete_at:
        return "probe", probe
    return "complete", complete

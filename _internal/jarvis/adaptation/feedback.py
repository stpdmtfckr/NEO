# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Feedback signal detection (spec section 7).

Turns what the user actually said into a graded correction signal.  Nothing else
in the package is allowed to invent a ``STRONG`` signal — it must come from here,
i.e. from the user's own words.

Three tiers, exactly as the spec requires:

``STRONG``   «нет, я сказал Brakk» / «я спрашивал про Warframe» / «no, I said X»
``MEDIUM``   the user immediately repeated the same request with one token changed
``WEAK``     NEO's own contextual inference (produced by the resolver, not here)
"""

import re
from dataclasses import dataclass, field
from typing import Any

from .events import FeedbackStrength
from .phonetics import combined_similarity
from jarvis.gaming.normalize import norm, tokens

__all__ = ["CorrectionSignal", "detect_correction", "detect_repeat_correction",
           "detect_routing_correction", "looks_like_denial"]


# «нет, я сказал X» / «я говорил X» / «no I said X». The captured tail is what
# the user insists they actually uttered.
_SAID_RE = re.compile(
    r"(?:^|[,.;!?]\s*|\s)(?:нет[,\s]+)?(?:я\s+(?:же\s+)?(?:сказал[а]?|говорил[а]?|произнёс|произнес)"
    r"|i\s+said|i\s+meant|я\s+имел[а]?\s+в\s+виду|имелось\s+в\s+виду)\s+(?P<tail>.{1,80})$",
    re.I | re.U,
)

# «нет, я спрашивал про X» / «я про X» / «no, I asked about X»
_ASKED_RE = re.compile(
    r"(?:^|[,.;!?]\s*|\s)(?:нет[,\s]+)?(?:я\s+(?:же\s+)?(?:спрашивал[а]?|спросил[а]?)\s+(?:про|о|об)"
    r"|я\s+про|речь\s+(?:шла|идёт|идет)\s+(?:про|о|об)"
    r"|i\s+asked\s+about|i\s+was\s+asking\s+about)\s+(?P<tail>.{1,80})$",
    re.I | re.U,
)

# «нет, это команда Windows» / «это не игра» / «это про музыку»
_DOMAIN_RE = re.compile(
    r"(?:^|[,.;!?]\s*|\s)(?:нет[,\s]+)?(?:это|эта|этот)\s+(?:не\s+)?(?P<neg>не\s+)?"
    r"(?:команда|запрос|вопрос|про)?\s*(?P<domain>[\w\s\-]{2,40})$",
    re.I | re.U,
)

_DENIAL_RE = re.compile(r"^\s*(?:нет|не\s+то|неверно|неправильно|no|nope|wrong|not\s+that)\b", re.I | re.U)

# Words that are never the *content* of a correction, only its frame.
_FRAME_WORDS = {
    "нет", "не", "то", "это", "именно", "а", "же", "ну", "вот", "там",
    "no", "not", "that", "it", "was", "the", "a", "an",
}


@dataclass(slots=True)
class CorrectionSignal:
    """One user-supplied correction, ready to be folded into the store."""

    kind: str                      # "speech" | "routing"
    corrected: str                 # what the user insists they said/meant
    strength: FeedbackStrength = FeedbackStrength.STRONG
    replaced: str = ""             # what NEO heard, when it can be identified
    reason: str = ""
    confidence: float = 0.0
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "corrected": self.corrected, "replaced": self.replaced,
            "strength": self.strength.label, "reason": self.reason,
            "confidence": round(self.confidence, 3), "payload": dict(self.payload),
        }


def looks_like_denial(text: str) -> bool:
    return bool(_DENIAL_RE.match(str(text or "")))


def _clean_tail(tail: str) -> str:
    """Strip the framing words from a captured correction tail."""
    words = [w for w in str(tail or "").strip().strip(".!?,;:").split() if w]
    while words and norm(words[0]) in _FRAME_WORDS:
        words.pop(0)
    while words and norm(words[-1]) in _FRAME_WORDS:
        words.pop()
    return " ".join(words[:4]).strip()


def _best_replaced(corrected: str, previous_text: str) -> tuple[str, float]:
    """Find which token of the previous utterance the correction replaces.

    Without this the store would learn ``"" -> "Brakk"``, which is not a rule.
    The winning token is the one that sounds most like the correction — that is
    precisely the ASR error being repaired.
    """
    best, best_score = "", 0.0
    for token in tokens(previous_text):
        score = combined_similarity(token, corrected)
        if score > best_score:
            best, best_score = token, score
    return best, best_score


def detect_correction(text: str, *, previous_text: str = "") -> CorrectionSignal | None:
    """Explicit, spoken correction of a mis-transcribed word (STRONG)."""
    raw = str(text or "").strip()
    if not raw:
        return None
    match = _SAID_RE.search(raw)
    if match is None:
        return None
    corrected = _clean_tail(match.group("tail"))
    if not corrected or len(norm(corrected)) < 2:
        return None
    replaced, score = _best_replaced(corrected, previous_text) if previous_text else ("", 0.0)
    if replaced and norm(replaced) == norm(corrected):
        # NEO already heard it correctly; nothing to learn.
        return None
    return CorrectionSignal(
        kind="speech",
        corrected=corrected,
        replaced=replaced,
        strength=FeedbackStrength.STRONG,
        reason="user_said_explicit",
        confidence=score,
        payload={"phrase": raw},
    )


def detect_routing_correction(text: str) -> CorrectionSignal | None:
    """«нет, я спрашивал про игру» / «это команда Windows» (STRONG)."""
    raw = str(text or "").strip()
    if not raw:
        return None
    match = _ASKED_RE.search(raw)
    if match is not None:
        target = _clean_tail(match.group("tail"))
        if target:
            return CorrectionSignal(
                kind="routing", corrected=target, strength=FeedbackStrength.STRONG,
                reason="user_named_subject", payload={"phrase": raw},
            )
    if not looks_like_denial(raw):
        return None
    match = _DOMAIN_RE.search(raw)
    if match is None:
        return None
    domain = _clean_tail(match.group("domain"))
    if not domain or len(norm(domain)) < 3:
        return None
    return CorrectionSignal(
        kind="routing", corrected=domain, strength=FeedbackStrength.STRONG,
        reason="user_named_domain", payload={"phrase": raw, "negated": bool(match.group("neg"))},
    )


def detect_repeat_correction(
    text: str, previous_text: str, *, max_gap_seconds: float = 45.0, gap_seconds: float = 0.0,
) -> CorrectionSignal | None:
    """The user simply said it again with one word fixed (MEDIUM).

    This is the most common real-world correction and the user never labels it,
    so it is inferred structurally: same sentence shape, exactly one token
    differs, and that token sounds like the one it replaced.
    """
    if gap_seconds > max(1.0, float(max_gap_seconds)):
        return None
    now_tokens = tokens(text)
    was_tokens = tokens(previous_text)
    if not now_tokens or not was_tokens:
        return None
    if abs(len(now_tokens) - len(was_tokens)) > 1:
        return None
    changed = [(a, b) for a, b in zip(was_tokens, now_tokens) if a != b]
    if len(changed) != 1:
        return None
    was, now = changed[0]
    similarity = combined_similarity(was, now)
    # Too similar means a stemming artefact; too different means a new question.
    if not (0.45 <= similarity < 0.999):
        return None
    return CorrectionSignal(
        kind="speech", corrected=now, replaced=was,
        strength=FeedbackStrength.MEDIUM, reason="immediate_rephrase",
        confidence=similarity, payload={"phrase": str(text or "")},
    )

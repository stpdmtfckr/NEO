# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Strict ownership predicates for game session/player-state versus game information.

These helpers deliberately require positive evidence.  A foreground game, or
generic words such as ``сколько``, ``время`` and ``персонаж``, are never enough
to route a turn into session/player-state.
"""

import re

from .normalize import norm

_SESSION_NOUN_RE = re.compile(r"\b(?:сесси\w*|session\w*)\b", re.I)
_FIRST_PERSON_RE = re.compile(r"\b(?:я|мне|меня|мой|моя|мо[её]|мои|my|i)\b", re.I)
_PLAY_VERB_RE = re.compile(r"\b(?:играю|играл\w*|играть|запустил\w*|запускал\w*|начал\w*\s+игр\w*|playing|played|started)\b", re.I)
_TIME_QUERY_RE = re.compile(r"\b(?:сколько\s+(?:времени\s+)?|как\s+долго|когда|время|duration|how\s+long|when)\b", re.I)
_GAME_CONTEXT_RE = re.compile(r"\b(?:игр\w*|game\w*)\b", re.I)
_SESSION_PANEL_RE = re.compile(r"\b(?:покажи|отобрази|выведи|открой|show|display|open)\w*\b", re.I)

_POSSESSIVE_RE = re.compile(r"\b(?:мой|моя|мо[её]|мои|моего|моей|у\s+меня|my|mine|i\s+have)\b", re.I)
_PLAYER_STATE_OBJECT_RE = re.compile(
    r"\b(?:инвентар\w*|inventory\w*|экипиров\w*|loadout\w*|сохранен\w*|сохранён\w*|save\s*data|"
    r"прогресс\w*|progress\w*|билд\w*|build\w*|экипирован\w*|equipped\w*|"
    r"надет\w*|в\s+рук\w*|патрон\w*|боеприпас\w*|ammo\w*|"
    r"сколько\s+у\s+меня|what\s+do\s+i\s+have)\b", re.I
)


def is_explicit_game_session_query(text: str) -> bool:
    """Return True only for a genuine request about the user's play session."""
    t = norm(text)
    if not t:
        return False
    # Explicitly asking to show/current game session is sufficient.
    if _SESSION_NOUN_RE.search(t):
        if _FIRST_PERSON_RE.search(t) or _GAME_CONTEXT_RE.search(t) or _SESSION_PANEL_RE.search(t):
            return True
    # Elapsed-time questions require first-person + an actual play/start verb.
    if _TIME_QUERY_RE.search(t) and _FIRST_PERSON_RE.search(t) and _PLAY_VERB_RE.search(t):
        return True
    # Common concise Russian form: «сколько я уже играю».
    if re.search(r"\b(?:сколько|как\s+долго)\b[^?]{0,24}\bя\b[^?]{0,24}\bиграю\b", t, re.I):
        return True
    return False


def is_explicit_player_state_query(text: str) -> bool:
    """Return True only for user-specific mutable game state.

    ``персонаж`` alone is intentionally *not* state.  «Какие способности у
    персонажа Veteran?» is general game information.
    """
    t = norm(text)
    if not t:
        return False
    if _PLAYER_STATE_OBJECT_RE.search(t) and (_POSSESSIVE_RE.search(t) or re.search(r"\b(?:текущ\w*|сейчас|current)\b", t, re.I)):
        return True
    return False



# Broad *shape* only. This never grants a factual answer by itself; it is used
# to choose between the closed game-information namespace and unrestricted chat.
# When a supported game is active, an unresolved information-seeking turn is
# safer to clarify/fail closed than to let a general LLM invent game facts.
_INFORMATION_SEEKING_RE = re.compile(
    r"(?:^|\b)(?:кто|что|где|куда|откуда|как|какой|какая|какое|какие|чей|чья|чьи|"
    r"почему|зачем|сколько|когда|чем|для\s+чего|расскажи\w*|объясни\w*|поясни\w*|"
    r"покажи\w*|перечисли\w*|сравни\w*|посоветуй\w*|нужно\s+ли|можно\s+ли|"
    r"who|what|where|how|which|why|when|tell|explain|show|list|compare)(?:\b|$)",
    re.I,
)

# Explicit escape hatch: the user can always say the question is not about the
# foreground game. We deliberately keep this small and semantic rather than
# attempting to enumerate every possible non-game subject.
_EXPLICIT_GENERAL_RE = re.compile(
    r"\b(?:не\s+про\s+игр\w*|не\s+об\s+игр\w*|не\s+про\s+darktide|"
    r"вообще\s+не\s+про\s+игр\w*|общий\s+вопрос|вопрос\s+не\s+по\s+игр\w*|"
    r"not\s+about\s+(?:the\s+)?game|general\s+question)\b", re.I,
)

def is_information_seeking_turn(text: str) -> bool:
    """True for a question/explanation request, never for session/state.

    This is intentionally domain-agnostic. Callers must still honor an
    authoritative external-domain claim. Its purpose is fail-closed safety: if
    Darktide is active and V5 cannot prove whether an information request is
    about the game, ask/decline rather than give Qwen permission to invent.
    """
    t = norm(text)
    if not t or is_explicit_game_session_query(t) or is_explicit_player_state_query(t):
        return False
    if _EXPLICIT_GENERAL_RE.search(t):
        return False
    return bool(
        _INFORMATION_SEEKING_RE.search(t)
        or str(text or "").strip().endswith("?")
    )

def is_explicit_general_non_game_query(text: str) -> bool:
    return bool(_EXPLICIT_GENERAL_RE.search(norm(text)))

def is_game_information_shape(text: str) -> bool:
    """Cheap positive signal used only to protect knowledge from session theft."""
    t = norm(text)
    if not t or is_explicit_game_session_query(t) or is_explicit_player_state_query(t):
        return False
    return bool(re.search(
        r"\b(?:кто|что|какой|какая|какое|какие|где|куда|откуда|как|зачем|почему|сколько|"
        r"талант\w*|способност\w*|класс\w*|оруж\w*|предмет\w*|ресурс\w*|эффект\w*|"
        r"урон\w*|бафф\w*|перк\w*|благослов\w*|персонаж\w*|what|who|which|where|how|why)\b",
        t, re.I,
    ))


__all__ = [
    "is_explicit_game_session_query", "is_explicit_player_state_query",
    "is_game_information_shape", "is_information_seeking_turn",
    "is_explicit_general_non_game_query",
]

"""186 CATALOG185-COVERAGE-01: every registered action is either published or classified."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis.runtime.capability_scope186 import PUBLISHED_ONLY, SCOPE
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    with external_io_blocked():
        value = AtlasSession(tmp_path_factory.mktemp("scope") / "run")
        value.reset_case("ru")
        yield value
        value.finish()


def _registered(session) -> set[str]:
    ids = set()
    for tool in session.agent.tools._tools.values():
        enum = ((tool.parameters or {}).get("properties", {}).get("action", {}) or {}).get("enum") or []
        ids.update(f"{tool.name}.{action}" for action in enum)
    return ids


@pytest.mark.parametrize("code", ["ru", "en", "de", "fr", "es", "it", "pt"])
def test_every_registered_action_is_published_or_classified(session, code):
    published = set(json.loads((ROOT / f"jarvis/locales/commands/{code}.json").read_text(encoding="utf-8"))["action_phrases"])
    registered = _registered(session)
    assert registered - published - set(SCOPE) == set()
    assert published - registered <= set(PUBLISHED_ONLY)          # translate: actionless schema
    assert set(SCOPE) <= registered                                 # no stale classification


def test_scopes_are_explicit():
    for action_id, row in {**SCOPE, **PUBLISHED_ONLY}.items():
        assert row["scope"] in {"public", "context", "internal"}, action_id
        if row["scope"] == "public":
            assert row.get("examples"), action_id
        if row["scope"] == "internal":
            assert row.get("via"), action_id


EXAMPLES = [(action_id, code, phrase) for action_id, row in {**SCOPE, **PUBLISHED_ONLY}.items()
            for code, phrase in (row.get("examples") or {}).items()]


@pytest.mark.parametrize("action_id,code,phrase", EXAMPLES)
def test_public_examples_reach_their_action(session, action_id, code, phrase):
    with external_io_blocked():
        session.reset_case(code)
        session.agent.handle(phrase)
    tool, _, action = action_id.partition(".")
    calls = [(c["tool"], str((c["arguments"] or {}).get("action", ""))) for c in session.calls]
    assert any(t == tool and (a == action or (tool == "translate" and not a)) for t, a in calls), (phrase, calls)

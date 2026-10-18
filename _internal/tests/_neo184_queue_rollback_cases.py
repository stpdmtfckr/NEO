"""Finite, synchronous rollback cases shared by pytest and the inert NB13 runner.

No imports of NEO, pytest, providers, native libraries, or process/thread owners.
The caller supplies the already-contained adapter and a patch function.
"""
from types import SimpleNamespace


CASES = (
    ("skip_clear", "skip", "clear"),
    ("skip_add", "skip", "add"),
    ("skip_unchanged", "skip", "unchanged"),
    ("skip_next", "skip", "next"),
    ("skip_clear_add", "skip", "clear_add"),
    ("skip_add_clear", "skip", "add_clear"),
    ("skip_duplicates", "skip", "duplicates"),
    ("skip_repeated_reference", "skip", "repeated_reference"),
    ("skip_repeated_anchor", "skip", "repeated_anchor"),
    ("skip_single_add", "skip", "single_add"),
    ("skip_single_next", "skip", "single_next"),
    ("skip_youtube_next", "skip", "youtube_next"),
    ("skip_forced_tail", "skip", "forced_tail"),
    ("skip_cancel_add", "skip", "cancel_add"),
    ("skip_cancel_clear", "skip", "cancel_clear"),
    ("skip_pause_add", "skip", "pause_add"),
    ("skip_stop_add", "skip", "stop_add"),
    ("skip_new_owner", "skip", "new_owner"),
    ("previous_unchanged", "previous", "unchanged"),
    ("previous_clear", "previous", "clear"),
    ("previous_add", "previous", "add"),
    ("previous_next", "previous", "next"),
    ("previous_clear_add", "previous", "clear_add"),
    ("previous_add_clear", "previous", "add_clear"),
    ("previous_duplicates", "previous", "duplicates"),
    ("previous_youtube_next", "previous", "youtube_next"),
    ("previous_cancel_add", "previous", "cancel_add"),
    ("previous_cancel_clear", "previous", "cancel_clear"),
    ("previous_pause_add", "previous", "pause_add"),
    ("previous_stop_add", "previous", "stop_add"),
    ("previous_new_owner", "previous", "new_owner"),
)


def exercise_case(dn, obj, state, profile, patch, spec):
    name, operation, edit = spec
    current = {"title": "A", "track_id": "A", "query": "A"}
    first = {"title": "B", "track_id": "B", "query": "B", "_source_type": "youtube",
             "_source_url": "https://www.youtube.com/watch?v=syntheticB1"}
    second = {"title": "C", "track_id": "C", "query": "C"}
    previous = {"title": "P", "track_id": "P", "query": "P", "_source_type": "youtube",
                "_source_url": "https://www.youtube.com/watch?v=syntheticP1"}
    original = [first, second]
    if edit == "duplicates":
        original = [first, dict(first), second] if operation == "skip" else [dict(current), second]
    elif edit == "repeated_reference":
        original = [first, second, first]
    elif edit == "repeated_anchor":
        first["_force_next"] = True
        original = [second, first, {"title": "E", "track_id": "E", "query": "E"}, second]
    elif edit.startswith("single_"):
        original = [first]
    elif edit == "forced_tail":
        first["_force_next"] = True
        original = [second, first]
    history = [] if name == "skip_clear" else [{"title": "H", "track_id": "H", "query": "H"}]
    if operation == "previous":
        history.append(previous)
    state.update(current=current, items=list(original), history=list(history), source_mode="playlist",
                 source_title="Synthetic playlist", source_data={"synthetic": True}, transport_faulted=False)
    callbacks, edits, jobs, resolves, fake_controls, additions = [], [], [], [], [], []
    newer = {"title": "N", "track_id": "N", "query": "N", "generation": 999}
    newer_items = [{"title": "E", "track_id": "E", "query": "E"}]
    newer_history = [{"title": "J", "track_id": "J", "query": "J"}]

    def resolve(query):
        resolves.append(query)
        return {"id": ("B" if operation == "skip" else "A") if edit == "duplicates" else "D",
                "title": "D", "artist": "Synthetic"}

    def action(action_name, **kwargs):
        before_list = state["items"]
        before = list(state["items"])
        result = obj.execute(action_name, server=profile["name"], **kwargs)
        for item in state["items"]:
            if not any(item is old for old in before):
                additions.append(item)
        edits.append({"action": action_name, "verified": result.get("verified"),
                      "queued": result.get("queued"), "items": list(state["items"]),
                      "replaced_list": state["items"] is not before_list,
                      "source": (state["source_mode"], state["source_title"], dict(state["source_data"]))})

    def failed_download(url):
        callbacks.append({"url": url, "before": list(state["items"])})
        if edit in {"clear", "cancel_clear", "clear_add"}:
            action("music_queue_clear")
        if edit in {"add", "clear_add", "add_clear", "duplicates", "repeated_reference", "repeated_anchor", "single_add", "forced_tail",
                    "cancel_add", "pause_add", "stop_add"}:
            action("music_queue_add", query="D")
        if edit in {"next", "single_next"}:
            action("music_queue_next", query="D")
        if edit == "youtube_next":
            action("music_queue_next", query="https://www.youtube.com/watch?v=syntheticD1")
        if edit == "add_clear":
            action("music_queue_clear")
        if edit.startswith("cancel_"):
            dn.cancel_transport_requests(obj)
        if edit in {"pause_add", "stop_add"}:
            action("music_pause" if edit == "pause_add" else "music_stop")
        if edit == "new_owner":
            # A newer transport already owns all three fields; the stale
            # rollback must not touch any of them, even on ordinary failure.
            with obj._queue_lock:
                state.update(current=newer, items=list(newer_items), history=list(newer_history), generation=999)
        raise ConnectionError("NB13 synthetic preparation failure")

    def submit(key, job):
        jobs.append(key.split(":", 1)[0])
        job()
        return True

    def fake_voice_action(profile, action_name, **kwargs):
        fake_controls.append(action_name)
        return {"verified": True, "synthetic": True}

    patch(obj._yandex, "resolve_track", resolve)
    patch(obj._youtube, "download_audio", failed_download)
    patch(dn, "PREFETCH", SimpleNamespace(submit=submit))
    if edit == "youtube_next":
        patch(obj, "_background_enabled", lambda: True)
        patch(obj, "_ordered_profiles_for_text_media", lambda *a: [profile])
    if edit in {"pause_add", "stop_add"}:
        patch(obj, "_background_for_profile", lambda *a: SimpleNamespace(voice_action=fake_voice_action))
    result, failure = None, None
    try:
        result = obj.execute("music_" + operation, server=profile["name"])
    except ConnectionError as exc:
        failure = str(exc)
    # Prove the callback and acknowledged edits actually occurred before
    # judging the rollback. Provider catches cannot hide assertion failures.
    assert len(callbacks) == len(jobs) == 1, (callbacks, jobs)
    expected_url = (first if operation == "skip" else previous)["_source_url"]
    assert callbacks[0]["url"] == expected_url
    pending = callbacks[0]["before"]
    if operation == "skip":
        assert len(pending) == len(original) - 1
        expected_after_pop = list(original)
        expected_after_pop.pop(next(i for i, item in enumerate(original) if item is first))
        assert all(actual is expected for actual, expected in zip(pending, expected_after_pop))
    else:
        assert pending[0] == current and pending[0] is not current
        assert all(actual is expected for actual, expected in zip(pending[1:], original))
    expected_actions = {
        "unchanged": [], "clear": ["music_queue_clear"], "add": ["music_queue_add"],
        "next": ["music_queue_next"], "clear_add": ["music_queue_clear", "music_queue_add"],
        "add_clear": ["music_queue_add", "music_queue_clear"], "duplicates": ["music_queue_add"],
        "repeated_reference": ["music_queue_add"], "repeated_anchor": ["music_queue_add"],
        "single_add": ["music_queue_add"], "single_next": ["music_queue_next"],
        "youtube_next": ["music_queue_next"], "forced_tail": ["music_queue_add"],
        "cancel_add": ["music_queue_add"], "cancel_clear": ["music_queue_clear"],
        "pause_add": ["music_queue_add", "music_pause"], "stop_add": ["music_queue_add", "music_stop"],
        "new_owner": [],
    }[edit]
    assert [row["action"] for row in edits] == expected_actions
    assert all(row["verified"] is True for row in edits)
    expected_pending = list(pending)
    for row in edits:
        if row["action"] == "music_queue_clear":
            assert row["items"] == [] and row["source"] == ("", "", {})
            expected_pending = []
        elif row["action"] in {"music_queue_add", "music_queue_next"}:
            assert row["queued"] is True
            assert len(additions) == 1, "Expected exactly one acknowledged addition"
            expected_pending = (expected_pending + additions if row["action"] == "music_queue_add"
                                else additions + expected_pending)
        elif row["action"] == "music_stop":
            expected_pending = []
        assert len(row["items"]) == len(expected_pending) and all(
            actual is expected for actual, expected in zip(row["items"], expected_pending))
    assert resolves == (["D"] if "music_queue_add" in expected_actions or (
        "music_queue_next" in expected_actions and edit != "youtube_next") else [])
    cancelled = edit.startswith("cancel_") or edit in {"pause_add", "stop_add"}
    if cancelled:
        assert failure is None and result["cancelled"] and not result["verified"]
    else:
        assert failure is not None and result is None
    assert fake_controls == (["pause"] if edit == "pause_add" else ["stop"] if edit == "stop_add" else [])
    if edit == "youtube_next":
        assert edits[0]["replaced_list"], "Public YouTube insertion must exercise list replacement"

    expected_items = list(original)
    if edit in {"clear", "add_clear", "cancel_clear", "stop_add"}:
        expected_items = []
    elif edit == "clear_add":
        expected_items = additions
    elif edit in {"next", "single_next", "youtube_next"}:
        expected_items = additions + original
    elif additions:
        expected_items = original + additions
    expected_current = None if edit == "stop_add" else current
    # Stop advances generation and takes ownership of the live history too.
    expected_history = history[:-1] if edit == "stop_add" and operation == "previous" else history
    if edit == "new_owner":
        expected_current, expected_items, expected_history = newer, newer_items, newer_history
    checks = {
        "identity_order_preserved": len(state["items"]) == len(expected_items) and all(
            actual is expected for actual, expected in zip(state["items"], expected_items)),
        "current_preserved": state["current"] == expected_current,
        "history_preserved": state["history"] == expected_history,
        "fault_state_preserved": bool(state["transport_faulted"]) == (not cancelled and edit != "new_owner"),
        "pause_preserved": bool(state.get("manual_paused")) == (edit in {"pause_add", "stop_add"}),
    }
    cleared = edit in {"clear", "clear_add", "add_clear", "cancel_clear", "stop_add"}
    checks["source_metadata_preserved"] = (
        state["source_mode"], state["source_title"], state["source_data"]
    ) == (("", "", {}) if cleared else ("playlist", "Synthetic playlist", {"synthetic": True}))
    if not cancelled:
        expected_persisted = {
            "previous": [obj._stable_entry(item) for item in expected_history[-5:]],
            "current": obj._stable_entry(expected_current),
            "upcoming": [obj._stable_entry(item) for item in expected_items[:5]],
            "source_mode": "" if cleared else "playlist",
        }
        persisted = obj._session_stores[obj._profile_key(profile)].load()
        checks["persisted_payload_preserved"] = all(
            persisted.get(key) == value for key, value in expected_persisted.items())

    def labels(items):
        return [item.get("track_id") or item.get("_source_url") for item in items]

    return {"case": name, "status": "PASS" if all(checks.values()) else "CONFIRMED_DEFECT",
            "checks": checks, "expected_items": labels(expected_items), "actual_items": labels(state["items"]),
            "edit_inside_preparation": [{**row, "items": labels(row["items"])} for row in edits],
            "synthetic_download_calls": len(callbacks), "yandex_resolve_queries": resolves,
            "prefetch_jobs": jobs, "synthetic_transport_controls": fake_controls,
            "native_play_or_stop_calls": 0, "public_result_cancelled": cancelled}

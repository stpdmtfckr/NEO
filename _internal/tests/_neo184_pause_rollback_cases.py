"""Two public, synchronous, already-paused transport-failure cases.

Definitions only. The caller supplies the reviewed contained adapter fixture.
No success-path playback, native owner, worker, provider or pytest is imported.
"""
from types import SimpleNamespace
import json


ORIGINAL_CASES = (
    ("paused_skip_failure", "skip"),
    ("paused_previous_failure", "previous"),
)


class UnexpectedTransport(BaseException):
    """Forbidden synthetic controls must not be caught as provider failures."""


def exercise_original_case(dn, obj, state, profile, patch, spec):
    name, operation = spec
    current = {"title": "A", "track_id": "A", "query": "A", "generation": 7,
               "started_at": 1.0, "_paused_seconds": 3.25}
    first = {"title": "B", "track_id": "B", "query": "B", "_source_type": "youtube",
             "_source_url": "https://www.youtube.com/watch?v=syntheticB1"}
    second = {"title": "C", "track_id": "C", "query": "C"}
    previous = {"title": "P", "track_id": "P", "query": "P", "_source_type": "youtube",
                "_source_url": "https://www.youtube.com/watch?v=syntheticP1"}
    original_items = [first, second]
    original_history = [{"title": "H", "track_id": "H", "query": "H"}, previous]
    source_data = {"synthetic": True}
    state.update(current=current, items=list(original_items), history=list(original_history),
                 generation=7, source_mode="playlist", source_title="Synthetic playlist",
                 source_data=dict(source_data), transport_faulted=False, manual_paused=False)
    controls, playback_attempts, callbacks, jobs, bot_lookups = [], [], [], [], []
    transport = {"paused": False}

    def voice_action(received_profile, action_name, **kwargs):
        controls.append(action_name)
        if received_profile != profile or action_name != "pause" or len(controls) != 1:
            raise UnexpectedTransport("Only one inert public Pause is permitted: " + action_name)
        transport["paused"] = True
        return {"verified": True, "synthetic": True, "paused": True, "playing": False}

    def play_url(*args, **kwargs):
        playback_attempts.append("play_url")
        raise UnexpectedTransport("Failed preparation must not reach the synthetic playback edge")

    bot = SimpleNamespace(voice_action=voice_action, play_url=play_url)

    def background_for_profile(received_profile):
        bot_lookups.append(dict(received_profile))
        return bot

    patch(obj, "_background_for_profile", background_for_profile)

    # Public Pause must actually establish the live flag and clock before the
    # switch starts; merely assigning manual_paused=True would miss this route.
    pause_result = obj.execute("music_pause", server=profile["name"])
    assert pause_result["verified"] is True and pause_result["paused"] is True, "public Pause must verify paused"
    assert controls == ["pause"] and len(bot_lookups) == 1 and transport["paused"], "public Pause must be the only inert control"
    assert state["manual_paused"] is True and state["current"] is current, "public Pause must establish live paused state for A"
    assert float(current["_pause_started_at"]) > 0, "public Pause must start the pause clock"
    assert current["_paused_seconds"] == 3.25, "public Pause must retain accumulated pause duration"
    assert state["items"] == original_items and state["history"] == original_history, "public Pause must retain queue/history"
    paused_current = dict(current)
    paused_manual_serial = state["manual_action_serial"]
    paused_generation = state["generation"]
    # latest_transport calls _supersede_music_request exactly once for this
    # new non-nested request. That hook increments every queue's manual serial.
    # Require this known increment and reject any additional invalidation.
    expected_request_manual_serial = paused_manual_serial + 1

    def failed_download(url):
        callbacks.append({"url": url, "pending": list(state["items"]),
                          "current": dict(state["current"]),
                          "manual_paused": state.get("manual_paused"),
                          "manual_action_serial": state["manual_action_serial"],
                          "transport_paused": transport["paused"]})
        raise ConnectionError("NB13 already-paused synthetic preparation failure")

    def submit(key, job):
        jobs.append(key.split(":", 1)[0])
        job()
        return True

    patch(obj._youtube, "download_audio", failed_download)
    patch(dn, "PREFETCH", SimpleNamespace(submit=submit))
    result, failure = None, None
    try:
        result = obj.execute("music_" + operation, server=profile["name"])
    except ConnectionError as exc:
        failure = str(exc)

    # Establish exact execution before judging rollback. A setup failure or an
    # unexpected transport edge is HARNESS_BLOCKED, never defect evidence.
    assert len(callbacks) == len(jobs) == 1, ("exactly one preparation callback/job required", callbacks, jobs)
    expected_target = first if operation == "skip" else previous
    observed = callbacks[0]
    assert observed["url"] == expected_target["_source_url"], "preparation must target the selected track URL"
    assert observed["current"]["starting"] is True, "preparation must observe a starting current entry"
    assert observed["current"]["track_id"] == expected_target["track_id"], "preparation must observe the selected track"
    assert observed["current"]["generation"] == paused_generation + 1, "switch must advance current generation exactly once"
    assert observed["manual_action_serial"] == expected_request_manual_serial, "new public switch must increment manual serial exactly once"
    assert observed["transport_paused"] is True, "inert transport must remain paused during preparation"
    if operation == "skip":
        assert len(observed["pending"]) == 1 and observed["pending"][0] is second, "Skip must temporarily leave only original C pending"
    else:
        assert len(observed["pending"]) == 3, "Previous must temporarily prepend current A to B,C"
        assert observed["pending"][0] == paused_current and observed["pending"][0] is not current, "Previous must prepend an exact copy of paused A"
        assert all(a is b for a, b in zip(observed["pending"][1:], original_items)), "Previous must retain original B,C occurrences"
    assert result is None and failure is not None, "failed preparation must surface ConnectionError without a result"
    assert controls == ["pause"] and playback_attempts == [] and len(bot_lookups) == 1, "failed switch must make no further transport calls"

    live_current = state.get("current") or {}
    persisted = obj._session_stores[obj._profile_key(profile)].load()
    expected_persisted = {
        "current": obj._stable_entry(paused_current),
        "upcoming": [obj._stable_entry(item) for item in original_items],
        "previous": [obj._stable_entry(item) for item in original_history],
        "source_mode": "playlist",
    }
    checks = {
        "manual_paused_preserved": state.get("manual_paused") is True,
        "pause_clock_preserved": live_current.get("_pause_started_at") == paused_current["_pause_started_at"]
            and live_current.get("_paused_seconds") == paused_current["_paused_seconds"],
        "current_preserved": live_current == paused_current,
        "queue_identity_order_preserved": len(state["items"]) == len(original_items)
            and all(a is b for a, b in zip(state["items"], original_items)),
        "history_preserved": state["history"] == original_history,
        "source_metadata_preserved": (state["source_mode"], state["source_title"], state["source_data"])
            == ("playlist", "Synthetic playlist", source_data),
        "manual_action_serial_matches_single_request": state["manual_action_serial"] == expected_request_manual_serial,
        "transport_fault_recorded": state["transport_faulted"] is True,
        "inert_transport_still_paused": transport["paused"] is True,
        "only_inert_pause_control": controls == ["pause"] and playback_attempts == [] and len(bot_lookups) == 1,
        "persisted_current_queue_history_preserved": all(persisted.get(key) == value
            for key, value in expected_persisted.items()),
    }
    return {
        "case": name,
        "status": "PASS" if all(checks.values()) else "CONFIRMED_DEFECT",
        "checks": checks,
        "public_actions": ["music_pause", "music_" + operation],
        "pause_seed_verified": True,
        "manual_paused_before_switch": True,
        "manual_paused_during_preparation": observed["manual_paused"],
        "manual_paused_after_failure": state.get("manual_paused"),
        "pause_clock_before": {key: paused_current[key] for key in ("_pause_started_at", "_paused_seconds")},
        "pause_clock_after": {key: live_current.get(key) for key in ("_pause_started_at", "_paused_seconds")},
        "persisted_payload": persisted,
        "persisted_pause_field_contract": "absent from current schema; not asserted",
        "synthetic_download_calls": len(callbacks),
        "prefetch_jobs": jobs,
        "synthetic_transport_controls": controls,
        "background_bot_lookups": len(bot_lookups),
        "native_play_or_stop_calls": 0,
        "synthetic_resume_play_stop_calls": 0,
        "failure_type": "ConnectionError",
        "success_transition_exercised": False,
    }


OWNERSHIP_CASES = tuple(
    ("pause_" + operation + "_" + label, operation, initially_paused, edit, edge_failure)
    for operation in ("skip", "previous")
    for label, initially_paused, edit, edge_failure in (
        ("paused_success", True, "success", False),
        ("playing_failure", False, "none", False),
        ("paused_cancel", True, "cancel", False),
        ("playing_pause_cancel", False, "pause", False),
        ("paused_resume_cancel", True, "resume", False),
        ("paused_stop_cancel", True, "stop", False),
        ("paused_new_playing_owner_failure", True, "new_playing_owner", False),
        ("playing_new_paused_owner_failure", False, "new_paused_owner", False),
        ("paused_new_playing_owner_cancel", True, "new_playing_owner_cancel", False),
        ("playing_new_paused_owner_cancel", False, "new_paused_owner_cancel", False),
        ("playing_pause_failure", False, "pause", True),
        ("paused_resume_failure", True, "resume", True),
        ("paused_staged_chain_failure", True, "chain", False),
        ("paused_foreign_request_cancel", True, "foreign_request", False),
        ("paused_foreign_pause_cancel", True, "foreign_pause", False),
    )
)
CASES = ORIGINAL_CASES + OWNERSHIP_CASES


def exercise_ownership_case(dn, obj, state, profile, patch, spec):
    """Public controls interleave synchronously inside the inert preparation edge."""
    name, operation, initially_paused, edit, edge_failure = spec
    foreign_profile = {"guild_id": "456", "name": "NB13 other synthetic"}
    foreign_state = None
    if edit.startswith("foreign_"):
        foreign_state = obj._queue_state(foreign_profile)
        foreign_state.update(current={"track_id": "F", "title": "F", "generation": 40},
                             manual_action_serial=10, pause_control_serial=20, manual_paused=False)
        patch(obj, "_background_profile", lambda server="": foreign_profile if server == foreign_profile["name"] else profile)
    current = {"title": "A", "track_id": "A", "query": "A", "generation": 7,
               "started_at": 1.0, "_paused_seconds": 3.25, "played_at": 4.0}
    first = {"title": "B", "track_id": "B", "query": "B", "_source_type": "youtube",
             "_source_url": "https://www.youtube.com/watch?v=syntheticB1"}
    second = {"title": "C", "track_id": "C", "query": "C"}
    previous = {"title": "P", "track_id": "P", "query": "P", "_source_type": "youtube",
                "_source_url": "https://www.youtube.com/watch?v=syntheticP1"}
    original_items = [first, second]
    original_history = [{"title": "H", "track_id": "H", "query": "H"}, previous]
    state.update(current=current, items=list(original_items), history=list(original_history),
                 generation=7, manual_action_serial=0, manual_paused=False,
                 source_mode="playlist", source_title="Synthetic playlist",
                 source_data={"synthetic": True}, transport_faulted=False)
    target = first if operation == "skip" else previous
    controls, starts, jobs, callbacks, control_receipts, published = [], [], [], [], [], []
    transport = {"paused": False, "stopped": False}
    new_current = {"title": "N", "track_id": "N", "query": "N", "generation": 999,
                   "_pause_started_at": 2.0, "_paused_seconds": 8.5}
    new_items = [{"title": "E", "track_id": "E", "query": "E"}]
    new_history = [{"title": "J", "track_id": "J", "query": "J"}]
    # An existing Python source is only an existence marker. The synthetic bot
    # checks this exact path and never opens, decodes or hands off its contents.
    marker_path = __file__
    expected_controls = (["pause"] if initially_paused else []) + (
        [edit] if edit in {"pause", "resume", "stop"} else ["pause"] if edit == "foreign_pause" else [])

    def voice_action(received_profile, action_name, **kwargs):
        controls.append(action_name)
        allowed_profile = foreign_profile if edit == "foreign_pause" and len(controls) > 1 else profile
        if received_profile != allowed_profile or controls != expected_controls[:len(controls)]:
            raise UnexpectedTransport("Unexpected inert public control: " + action_name)
        if received_profile == profile:
            transport.update(paused=action_name != "resume", stopped=action_name == "stop")
        return {"verified": True, "synthetic": True, "paused": transport["paused"]}

    def intervene():
        if edit in {"pause", "resume", "stop"}:
            before_serial = state["manual_action_serial"]
            before_request = obj._transport_request_serial
            before_generation = state["generation"]
            result = obj.execute("music_" + edit, server=profile["name"])
            control_receipts.append({"verified": result.get("verified"),
                "manual_before": before_serial, "manual_after": state["manual_action_serial"],
                "request_before": before_request, "request_after": obj._transport_request_serial,
                "generation_before": before_generation, "generation_after": state["generation"],
                "paused": state["manual_paused"]})
        elif edit == "cancel":
            dn.cancel_transport_requests(obj)
        elif edit in {"chain", "foreign_request"} and len(callbacks) == 1:
            # Stage the same two invalidations as a non-nested newer request's
            # header, without running any thread or pretending this is a full
            # concurrent wrapper call. The actual-wrapper regression is separate.
            dn.cancel_transport_requests(obj)
            obj._supersede_music_request()
        elif edit == "foreign_pause":
            result = obj.execute("music_pause", server=foreign_profile["name"])
            if not result.get("verified"):
                raise UnexpectedTransport("The other profile's public Pause did not verify")
        elif edit.startswith("new_"):
            with obj._queue_lock:
                # A changed current generation independently defeats stale
                # rollback, even if no explicit control increments the serial.
                state.update(current=new_current, items=list(new_items), history=list(new_history),
                             generation=999, manual_paused=edit.startswith("new_paused_owner"))
            if edit.endswith("_cancel"):
                dn.cancel_transport_requests(obj)

    def prepare(url):
        callbacks.append({"url": url, "manual": state["manual_action_serial"],
                          "request": obj._transport_request_serial,
                          "generation": state["generation"],
                          "paused": state["manual_paused"],
                          "starting": bool(state["current"].get("starting"))})
        if edit == "success" or edge_failure:
            return {"local_path": marker_path, "title": target["title"],
                    "video_id": target["track_id"]}
        intervene()
        raise ConnectionError("NB13 synthetic preparation failure")

    def play_url(received_profile, source, **kwargs):
        if received_profile != profile or source != marker_path or not (edit == "success" or edge_failure):
            raise UnexpectedTransport("Unexpected synthetic play edge")
        kwargs["before_start"]()
        starts.append(target["track_id"])
        if edge_failure:
            # A control followed by a provider failure must respect the newer
            # Boolean in the ordinary-exception rollback as well as cancellation.
            intervene()
            raise ConnectionError("NB13 synthetic pre-commit transport failure")
        transport.update(paused=False, stopped=False)
        return {"verified": True, "submitted": True, "synthetic": True,
                "playback_generation": 1, "title": target["title"]}

    def submit(key, job):
        jobs.append(key.split(":", 1)[0])
        job()
        return True

    def publish(*args, **kwargs):
        published.append(args)
        return "NB13-inert-event"

    def diagnostic(assertion):
        return {"assertion": assertion, "case": name, "operation": operation,
                "edit": edit, "callbacks": callbacks, "jobs": jobs,
                "controls": controls, "play_edges": starts,
                "published_events": len(published), "control_receipts": control_receipts,
                "manual_paused": state.get("manual_paused"),
                "manual_action_serial": state.get("manual_action_serial"),
                "pause_control_serial": state.get("pause_control_serial", 0),
                "request_serial": getattr(obj, "_transport_request_serial", 0),
                "current": dict(state.get("current") or {})}

    patch(obj, "_background_for_profile", lambda *a: SimpleNamespace(
        voice_action=voice_action, play_url=play_url))
    patch(obj._youtube, "download_audio", prepare)
    patch(dn, "PREFETCH", SimpleNamespace(submit=submit))
    patch(dn, "publish_now_playing", publish)
    if initially_paused:
        seeded = obj.execute("music_pause", server=profile["name"])
        assert seeded["verified"] and state["manual_paused"] is True, diagnostic("seeded['verified'] and state['manual_paused'] is True")
        assert current.get("_pause_started_at", 0) > 0, diagnostic("current.get('_pause_started_at', 0) > 0")
    original_current = dict(current)
    before_manual = state["manual_action_serial"]
    before_request = int(getattr(obj, "_transport_request_serial", 0))
    before_pause_control = int(state.get("pause_control_serial", 0))
    result, failure = None, None
    try:
        result = obj.execute("music_" + operation, server=profile["name"])
    except ConnectionError as exc:
        failure = str(exc)

    assert len(callbacks) == len(jobs) == 1, diagnostic('len(callbacks) == len(jobs) == 1')
    observed = callbacks[0]
    assert observed == {"url": target["_source_url"], "manual": before_manual + 1,
                        "request": before_request + 1, "generation": 8,
                        "paused": False, "starting": True}, diagnostic("observed == {'url': target['_source_url'], 'manual': before_manual + 1, 'request': before_request + 1, 'generation': 8, 'paused': False, 'starting': True}")
    assert controls == expected_controls, diagnostic('controls == expected_controls')
    assert starts == ([target["track_id"]] if edit == "success" or edge_failure else []), diagnostic("starts == ([target['track_id']] if edit == 'success' or edge_failure else [])")
    assert len(published) == (1 if edit == "success" else 0), diagnostic("len(published) == (1 if edit == 'success' else 0)")
    public_control = edit in {"pause", "resume", "stop"}
    assert len(control_receipts) == int(public_control), diagnostic('len(control_receipts) == int(public_control)')
    for receipt in control_receipts:
        assert receipt == {"verified": True, "manual_before": before_manual + 1,
            "manual_after": before_manual + 2, "request_before": before_request + 1,
            "request_after": before_request + 2, "generation_before": 8,
            "generation_after": 9 if edit == "stop" else 8, "paused": edit != "resume"}, diagnostic("receipt == {'verified': True, 'manual_before': before_manual + 1, 'manual_after': before_manual + 2, 'request_before': before_request + 1, 'request_after': before_request + 2, 'generation_before': 8, 'generation_after': 9 if edit == 'stop' else 8, 'paused': edit != 'resume'}")
    request_cancelled = public_control or edit in {"cancel", "chain", "foreign_request", "foreign_pause"} or edit.endswith("_cancel")
    cancelled = request_cancelled and not edge_failure
    if cancelled:
        assert failure is None and result["cancelled"] and not result["verified"], diagnostic("failure is None and result['cancelled'] and (not result['verified'])")
    elif edit == "success":
        assert failure is None and result["verified"] and result.get("submitted"), diagnostic("failure is None and result['verified'] and result.get('submitted')")
    else:
        assert result is None and failure is not None, diagnostic('result is None and failure is not None')

    expected_paused = initially_paused
    if edit in {"success", "resume"} or edit.startswith("new_playing_owner"):
        expected_paused = False
    elif edit in {"pause", "stop"} or edit.startswith("new_paused_owner"):
        expected_paused = True
    expected_current = original_current
    expected_items = original_items
    expected_history = original_history
    if edit == "success":
        expected_items = [second] if operation == "skip" else [original_current, *original_items]
        expected_history = [*original_history, original_current] if operation == "skip" else original_history[:-1]
    elif edit == "stop":
        expected_current, expected_items = None, []
        expected_history = original_history if operation == "skip" else original_history[:-1]
    elif edit.startswith("new_"):
        expected_current, expected_items, expected_history = new_current, new_items, new_history
    live_current = state["current"]
    successful_current = (live_current or {}).get("track_id") == target["track_id"] and (
        live_current or {}).get("generation") == 8 and not (live_current or {}).get("starting")
    checks = {
        "pause_owner_respected": state.get("manual_paused") is expected_paused,
        "manual_serial_not_rolled_back": state["manual_action_serial"] == before_manual + 1 + int(public_control or edit in {"chain", "foreign_request"}),
        "pause_control_serial_not_rolled_back": int(state.get("pause_control_serial", 0)) == before_pause_control + int(public_control),
        "request_serial_exact": obj._transport_request_serial == before_request + 1 + int(request_cancelled),
        "current_owner_respected": successful_current if edit == "success" else live_current == expected_current,
        "queue_order_preserved": [obj._stable_entry(x) for x in state["items"]]
            == [obj._stable_entry(x) for x in expected_items],
        "history_preserved": [obj._stable_entry(x) for x in state["history"]]
            == [obj._stable_entry(x) for x in expected_history],
        "fault_state_correct": bool(state["transport_faulted"]) == (
            not cancelled and edit != "success" and not edit.startswith("new_")),
        "transport_pause_not_changed_by_rollback": transport["paused"] == (
            edit != "resume" if public_control else False if edit == "success" else initially_paused),
        "source_metadata_preserved": (state["source_mode"], state["source_title"], state["source_data"])
            == (("", "", {}) if edit == "stop" else ("playlist", "Synthetic playlist", {"synthetic": True})),
    }
    if edit == "success":
        checks["success_clears_old_pause_clock"] = not any(
            key in live_current for key in ("_pause_started_at", "_paused_seconds"))
    elif edit in {"none", "cancel", "chain", "foreign_request", "foreign_pause"}:
        checks["owned_pause_clock_unchanged"] = all(
            (live_current or {}).get(key) == original_current.get(key)
            for key in ("_pause_started_at", "_paused_seconds"))
    if failure is not None:
        persisted = obj._session_stores[obj._profile_key(profile)].load()
        checks["existing_persisted_fields_preserved"] = all(persisted.get(key) == value for key, value in {
            "current": obj._stable_entry(expected_current),
            "upcoming": [obj._stable_entry(x) for x in expected_items],
            "previous": [obj._stable_entry(x) for x in expected_history],
            "source_mode": state["source_mode"],
        }.items())
    if foreign_state is not None:
        checks["foreign_state_is_independent"] = (
            foreign_state["manual_action_serial"] == 12
            and foreign_state["pause_control_serial"] == 20 + int(edit == "foreign_pause")
            and foreign_state["manual_paused"] is (edit == "foreign_pause")
            and foreign_state["current"]["generation"] == 40)
    chain_report = None
    if edit == "chain":
        # After the staged invalidation has cancelled request 1 and its rollback
        # has finished, a real second public request gets a fresh wrapper header.
        # Its serial is therefore +3, not the staged header's +2.
        paused_before_followup = state.get("manual_paused")
        second_result, second_failure = None, None
        try:
            second_result = obj.execute("music_" + operation, server=profile["name"])
        except ConnectionError as exc:
            second_failure = str(exc)
        chain_report = {"record": "STAGED_CHAIN_OBSERVATION", "case": name,
                        "staged_request_header_only": True, "actual_concurrent_wrappers": False,
                        "second_public_request_failed": second_failure is not None,
                        "manual_paused_before_followup": paused_before_followup,
                        "manual_paused_after": state.get("manual_paused"),
                        "download_callbacks": len(callbacks), "prefetch_jobs": len(jobs),
                        "manual_serial_after": state["manual_action_serial"],
                        "request_serial_after": obj._transport_request_serial,
                        "pause_control_serial_after": state.get("pause_control_serial", 0)}
        print(json.dumps(chain_report, sort_keys=True), flush=True)
        checks["staged_chain_restores_before_followup"] = paused_before_followup is True
        checks["staged_chain_second_failure_preserves_pause"] = (
            second_result is None and second_failure is not None and state.get("manual_paused") is True
            and state["current"] == original_current
            and state["manual_action_serial"] == before_manual + 3
            and obj._transport_request_serial == before_request + 3
            and state["pause_control_serial"] == before_pause_control
            and bool(state["transport_faulted"]))
        checks["staged_chain_queue_and_history_preserved"] = (
            len(state["items"]) == len(original_items)
            and all(a is b for a, b in zip(state["items"], original_items))
            and state["history"] == original_history)
        assert len(callbacks) == len(jobs) == 2, diagnostic('len(callbacks) == len(jobs) == 2')
        assert not starts and not published and controls == expected_controls, diagnostic('not starts and (not published) and (controls == expected_controls)')
    return {"case": name, "status": "PASS" if all(checks.values()) else "CONFIRMED_DEFECT",
            "checks": checks, "initially_paused": initially_paused,
            "later_control": edit, "control_receipts": control_receipts,
            "manual_paused_after": state.get("manual_paused"),
            "public_result_cancelled": cancelled, "prefetch_jobs": jobs,
            "synthetic_download_calls": len(callbacks), "synthetic_play_edges": len(starts),
            "synthetic_transport_controls": controls, "native_play_or_stop_calls": 0,
            "success_transition_exercised": edit == "success", "staged_chain": chain_report,
            "foreign_profile_control": edit if foreign_state is not None else None}


def exercise_case(dn, obj, state, profile, patch, spec):
    if len(spec) == 2:
        return exercise_original_case(dn, obj, state, profile, patch, spec)
    return exercise_ownership_case(dn, obj, state, profile, patch, spec)

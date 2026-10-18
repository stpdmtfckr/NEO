"""NEO 188 NEO-TG-001 / TZ-TELEGRAM-CONTROL-HISTORY-CLEANUP-01 on a fake Bot API."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from jarvis.integrations import telegram_cleanup188 as tc
from jarvis.integrations.telegram_remote import TelegramRemoteController

CHAT = "555"
NOW = 1_800_000_000.0


class FakeApi:
    def __init__(self):
        self.calls, self.fail_many, self.single = [], None, {}

    def __call__(self, method, payload=None, **_kw):
        self.calls.append((method, dict(payload or {})))
        if method == "deleteMessages":
            if self.fail_many:
                raise RuntimeError(self.fail_many)
            return True
        if method == "deleteMessage":
            error = self.single.get(int(payload["message_id"]))
            if error:
                raise RuntimeError(error)
            return True
        if method == "sendMessage":
            return {"message_id": 900 + len(self.calls), "date": int(time.time())}
        return True


def remote(tmp_path, api, *, state=None, allowed=(CHAT,)):
    controller = object.__new__(TelegramRemoteController)
    controller.config = SimpleNamespace(paths=SimpleNamespace(root=tmp_path), get=lambda key, default=None: default)
    controller._state_path = tmp_path / "telegram_remote_state.json"
    if state is not None:
        controller._state_path.write_text(json.dumps(state), encoding="utf-8")
    loaded = controller._load_state()
    controller._lock = threading.RLock()
    controller._tracked_messages = list(loaded.get("messages", []))
    controller._last_update_id = 0
    controller._last_cleanup_at = 0.0
    controller._cleanup_lock = threading.Lock()
    controller._send_barrier = False
    controller._cleanup_retry_at = 0.0
    controller._cleanup_report = {}
    controller._coverage_gap_at = float(loaded.get("coverage_gap_at", 0) or 0)
    controller._panel_by_chat, controller._view_by_chat = {}, {}
    controller._stop = threading.Event()
    controller._bot_token_cache = "12345:SECRET-TOKEN"
    controller._allowed_chat_ids = lambda: set(allowed)
    controller._api = api
    controller._show_home = lambda *a, **k: 0
    return controller


def entry(mid, age_hours, *, owned=True, kind="", bot="12345", chat=CHAT):
    item = {"chat_id": chat, "message_id": mid, "ts": time.time() - age_hours * 3600, "owned": owned,
            "origin": "bot" if owned else "user", "bot": bot}
    if kind:
        item["kind"] = kind
    return item


def test_tghc01_incoming_and_outgoing_known_ids_are_deleted(tmp_path):
    api = FakeApi()
    controller = remote(tmp_path, api)
    controller._tracked_messages = [entry(10, 1, owned=False), entry(11, 1), entry(12, 2, owned=False, kind="media"), entry(13, 3)]
    controller._panel_by_chat[CHAT] = 13
    controller._cleanup_chat(CHAT, recreate_home=False)
    deleted = [call[1]["message_ids"] for call in api.calls if call[0] == "deleteMessages"]
    assert deleted == [[10, 11, 12, 13]]
    assert controller._tracked_messages == [] and CHAT not in controller._panel_by_chat
    assert controller._cleanup_report["status"] == "complete" and controller._cleanup_report["deleted"] == 4
    saved = json.loads(controller._state_path.read_text(encoding="utf-8"))
    assert "SECRET" not in json.dumps(saved) and saved["ledger_version"] == 188


def test_user_messages_are_recorded_with_origin_and_without_text(tmp_path):
    controller = remote(tmp_path, FakeApi())
    controller._track_message(CHAT, 77, owned=False, kind="dice")
    item = controller._tracked_messages[-1]
    assert item["origin"] == "user" and item["kind"] == "dice" and item["bot"] == "12345"
    assert set(item) <= {"chat_id", "message_id", "ts", "owned", "origin", "bot", "kind"}


def test_tghc06_age_windows_ordinary_and_dice():
    now = time.time()
    rows = [{"message_id": 1, "ts": now - 47.7 * 3600}, {"message_id": 2, "ts": now - 48.0 * 3600},
            {"message_id": 3, "ts": now - 23.0 * 3600, "kind": "dice"}, {"message_id": 4, "ts": now - 24.5 * 3600, "kind": "dice"}]
    seen = []
    result = tc.run(rows, now, lambda batch: seen.append(batch), lambda mid: None)
    assert seen == [[1, 3]] and result.deleted == {1, 3} and result.expired == {2, 4}
    assert tc.status(result, coverage_gap=False) == "partial"
    assert "вручную" in tc.summary(dict(result.counts(), status="partial"), "ru")


def test_tghc08_rate_limit_keeps_ids_for_a_bounded_retry(tmp_path):
    api = FakeApi()
    api.fail_many = "Telegram API deleteMessages: Too Many Requests: retry after 37"
    controller = remote(tmp_path, api)
    controller._tracked_messages = [entry(20, 1), entry(21, 1, owned=False)]
    controller._cleanup_chat(CHAT, recreate_home=False)
    assert {x["message_id"] for x in controller._tracked_messages} == {20, 21}
    assert controller._cleanup_report["status"] == "partial" and controller._cleanup_report["pending"] == 2
    assert controller._cleanup_retry_at >= time.time() + 59
    assert not any(call[0] == "deleteMessage" for call in api.calls)      # no hammering after 429


def test_tghc09_absent_refused_and_temporary_are_distinct(tmp_path):
    api = FakeApi()
    api.fail_many = "Telegram API deleteMessages: Bad Request: message can't be deleted for everyone"
    api.single = {31: "Telegram API deleteMessage: Bad Request: message to delete not found",
                  32: "Telegram API deleteMessage: Bad Request: message can't be deleted",
                  33: "Read timed out"}
    controller = remote(tmp_path, api)
    controller._tracked_messages = [entry(30, 1), entry(31, 1), entry(32, 1), entry(33, 1)]
    controller._cleanup_chat(CHAT, recreate_home=False)
    report = controller._cleanup_report
    assert (report["deleted"], report["absent"], report["not_allowed"], report["pending"]) == (1, 1, 1, 1)
    assert [x["message_id"] for x in controller._tracked_messages] == [33]   # only the temporary failure stays
    assert report["status"] == "partial"


def test_tghc14_scope_other_chat_and_other_bot(tmp_path):
    api = FakeApi()
    controller = remote(tmp_path, api)
    controller._tracked_messages = [entry(40, 1, owned=False, chat="999"), entry(41, 1, chat="999"),
                                    entry(42, 1, bot="777")]
    controller._cleanup_chat("999", recreate_home=False)        # an old, no longer paired chat
    assert [call[1]["message_ids"] for call in api.calls if call[0] == "deleteMessages"] == [[41]]
    assert any(x["message_id"] == 40 for x in controller._tracked_messages)   # the user's message there is not touched
    controller._cleanup_chat(CHAT, recreate_home=False)
    assert not any(call[0] == "deleteMessages" and 42 in call[1]["message_ids"] for call in api.calls)
    assert controller._cleanup_report["not_allowed"] == 1


def test_tghc07_legacy_ledger_reports_unknown_coverage(tmp_path):
    state = {"last_update_id": 5, "messages": [entry(50, 1)]}
    (tmp_path / "telegram_remote_state.json").write_text(json.dumps(state), encoding="utf-8")
    loaded = json.loads((tmp_path / "telegram_remote_state.json").read_text(encoding="utf-8"))
    assert int(loaded.get("ledger_version", 0) or 0) < 188
    api = FakeApi()
    controller = remote(tmp_path, api)
    controller._coverage_gap_at = time.time()                     # what __init__ sets for an old ledger
    controller._cleanup_chat(CHAT, recreate_home=False)
    assert controller._cleanup_report["status"] == "unknown"
    text = controller.cleanup_summary("ru")
    assert "без известного ID" in text and "вручную" in text


def test_tghc12_lost_send_response_marks_a_gap_and_is_not_resent(tmp_path):
    calls = []

    def api(method, payload=None, **_kw):
        calls.append(method)
        raise RuntimeError("Read timed out")

    controller = remote(tmp_path, api)
    with pytest.raises(RuntimeError):
        controller._send_message(CHAT, "привет")
    assert calls == ["sendMessage"] and controller._coverage_gap_at > 0


def test_tghc04_no_message_after_the_final_cleanup_barrier(tmp_path):
    api = FakeApi()
    controller = remote(tmp_path, api)
    controller._send_barrier = True
    assert controller._send_message(CHAT, "после выхода") == []
    assert controller._send_photo(CHAT, str(tmp_path / "x.png")) == 0
    assert api.calls == []


def test_tghc02_no_two_passes_at_once(tmp_path):
    api = FakeApi()
    controller = remote(tmp_path, api)
    controller._tracked_messages = [entry(60, 1)]
    controller._cleanup_lock.acquire()
    try:
        controller._cleanup_chat(CHAT, recreate_home=False)
    finally:
        controller._cleanup_lock.release()
    assert api.calls == [] and controller._tracked_messages


def test_tghc13_message_tracked_during_a_pass_survives(tmp_path):
    controller = remote(tmp_path, None)

    def api(method, payload=None, **_kw):
        if method == "deleteMessages":
            controller._track_message(CHAT, 71)        # sent while the pass is running
        return True

    controller._api = api
    controller._tracked_messages = [entry(70, 1)]
    controller._cleanup_chat(CHAT, recreate_home=False)
    assert [x["message_id"] for x in controller._tracked_messages] == [71]


def test_tghc16_summaries_in_seven_languages():
    for code in ("ru", "en", "de", "fr", "es", "it", "pt"):
        complete = tc.summary({"status": "complete", "deleted": 3}, code)
        partial = tc.summary({"status": "partial", "deleted": 1, "pending": 1, "expired": 2}, code)
        assert "3" in complete and complete != partial
        assert tc.summary(None, code)


def test_tghc15_bounded_single_retries():
    calls = []

    def many(batch):
        raise RuntimeError("Telegram API deleteMessages: Bad Request: message can't be deleted")

    def one(mid):
        calls.append(mid)
        raise RuntimeError("Read timed out")

    rows = [{"message_id": i, "ts": time.time()} for i in range(1, 301)]
    result = tc.run(rows, time.time(), many, one, max_single=40)
    assert len(calls) == 40 and len(result.pending) == 300

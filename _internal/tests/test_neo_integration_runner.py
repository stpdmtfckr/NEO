import json
from unittest.mock import Mock
from integration_test_runner import PROVIDERS, probe


def test_read_only_probe_never_returns_secrets_or_account_data():
    session = Mock()
    session.get.return_value = Mock(status_code=200)
    session.get.return_value.json.return_value = {"id": "private-account", "email": "private@example.org"}
    spotify = next(p for p in PROVIDERS if p.name == "Spotify")
    report = probe(spotify, "private-token", session)
    assert report["status"] == "PASS"
    assert "private" not in json.dumps(report)
    assert session.get.call_args.kwargs["headers"]["Authorization"] == "Bearer private-token"
    session.post.assert_not_called()


def test_transport_exception_only_reports_type_not_token_url():
    session = Mock()
    session.get.side_effect = OSError("http://api.test?api_key=private-token")
    report = probe(PROVIDERS[0], "private-token", session)
    assert report["status"] == "UNAVAILABLE"
    assert report["reason"] == "OSError"
    assert "private" not in json.dumps(report)


def test_telegram_token_is_not_allowed_to_override_url():
    session = Mock()
    telegram = next(p for p in PROVIDERS if p.name == "Telegram")
    report = probe(telegram, "../getUpdates?foo=bar", session)
    assert report["status"] == "FAIL"
    session.get.assert_not_called()


def test_success_http_with_api_error_is_not_pass():
    session = Mock()
    session.get.return_value = Mock(status_code=200)
    session.get.return_value.json.return_value = {"error": "invalid credentials"}
    assert probe(PROVIDERS[0], "private-token", session)["status"] == "UNAVAILABLE"

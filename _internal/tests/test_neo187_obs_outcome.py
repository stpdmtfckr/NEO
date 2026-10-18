"""NEO 187: OBS unknown outcome after an attempted state-changing command."""
import pytest

from jarvis.integrations.obs_websocket184 import ObsClient, ObsError


def _client(reply):
    client = ObsClient.__new__(ObsClient)
    client.timeout = 1
    sent = []
    client._send = lambda message, deadline: sent.append(message)
    client.close = lambda: None
    client._event = lambda data: None

    def recv(deadline):
        value = reply(sent[-1]['d'])
        if isinstance(value, Exception):
            raise value
        return value
    client._recv = recv
    return client, sent


def _ack(d, result, code, kind=None):
    return {'op': 7, 'd': {'requestId': d['requestId'], 'requestType': kind or d['requestType'],
                           'requestStatus': {'result': result, 'code': code}, 'responseData': {}}}


@pytest.mark.parametrize('reply', [
    lambda d: {'op': 9, 'd': {}},                                  # wrong op after send
    lambda d: _ack(d, True, 100, kind='Other'),                    # mismatched type
    lambda d: _ack(d, True, 500),                                  # contradictory status
])
def test_broken_reply_after_mutation_is_uncertain(reply):
    client, sent = _client(reply)
    with pytest.raises(ObsError) as error:
        client._request('SaveReplayBuffer', {})
    assert error.value.key == 'uncertain' and len(sent) == 1        # never resent


def test_definite_rejection_keeps_its_code():
    client, _ = _client(lambda d: _ack(d, False, 500))
    with pytest.raises(ObsError) as error:
        client._request('SaveReplayBuffer', {})
    assert error.value.key == 'request_failed' and error.value.code == 500


def test_read_only_request_keeps_protocol_error():
    client, _ = _client(lambda d: {'op': 9, 'd': {}})
    with pytest.raises(ObsError) as error:
        client._request('GetReplayBufferStatus', {})
    assert error.value.key == 'protocol_error'


def test_positive_ack_returns_response():
    client, _ = _client(lambda d: _ack(d, True, 100))
    assert client._request('SaveReplayBuffer', {}) == {}

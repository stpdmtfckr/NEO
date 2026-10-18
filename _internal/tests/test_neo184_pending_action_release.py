from tests.test_neo184_delayed_actions import session,routed,manager,clock,no_external_io


def test_seventy_refusals_do_not_consume_action_capacity(routed,manager,clock):
    for _ in range(70):
        assert routed.agent.handle('shut down computer in10minutes').pending
        assert routed.agent.confirm_pending(False).outcome=='cancelled'
        assert routed.agent._pending_context is None
        assert not manager.list() and not routed.calls
    assert routed.agent.handle('shut down computer in10minutes').pending
    assert routed.agent.confirm_pending(True).outcome=='scheduled'
    assert len(manager.list())==1 and not routed.calls


def test_expired_confirmation_cannot_schedule_and_next_request_succeeds(routed,manager,clock):
    assert routed.agent.handle('shut down computer in10minutes').pending
    clock.mono+=121
    assert not routed.agent.confirm_pending(True).success
    assert routed.agent._pending_context is None and not manager.list() and not routed.calls
    assert routed.agent.handle('shut down computer in10minutes').pending
    assert routed.agent.confirm_pending(True).outcome=='scheduled'
    assert len(manager.list())==1 and not routed.calls


def test_risk_failure_clears_previous_preparation_and_allows_fresh_request(routed,manager,monkeypatch):
    assert routed.agent.handle('shut down computer in10minutes').pending
    original=routed.agent.tools.risk_for
    def fail(*a,**k):raise RuntimeError('synthetic risk lookup failure')
    monkeypatch.setattr(routed.agent.tools,'risk_for',fail)
    assert not routed.agent.handle('shut down computer in20minutes').success
    assert routed.agent._pending_context is None and not manager.list() and not routed.calls
    monkeypatch.setattr(routed.agent.tools,'risk_for',original)
    assert routed.agent.handle('shut down computer in10minutes').pending
    assert routed.agent.confirm_pending(True).outcome=='scheduled'
    assert len(manager.list())==1 and not routed.calls

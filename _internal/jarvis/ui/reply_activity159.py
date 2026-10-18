"""Keep background briefing decorations subordinate to the current user turn."""
from __future__ import annotations


def ambient_reply_activity(window, active: bool) -> None:
    """Follow existing typewriter ticks without replacing a newer user state."""
    robot = getattr(window, 'robot', None)
    if robot is None:
        return
    prior = getattr(window, '_ambient_robot_state159', None)
    state = robot.state()
    if not active:
        if prior is not None and state == 'typing' and not getattr(window, '_busy', False):
            robot.set_state(prior)
        window._ambient_robot_state159 = None
        return
    if getattr(window, '_busy', False) or state in {
        'listening', 'sleeping', 'error', 'thinking', 'working', 'speaking',
        'searching', 'reading', 'browsing', 'coding', 'scanning',
    }:
        window._ambient_robot_state159 = None
        return
    if prior is None:
        window._ambient_robot_state159 = state if state != 'typing' else 'idle'
        robot.set_state('typing')


def briefing_reply_activity(window, state: str = "waiting", *, finished: bool = False) -> None:
    robot = getattr(window, "robot", None)
    if robot is None:
        return
    # A late startup section must never change a newer command's animation.
    if (getattr(window, "_busy", False)
            or getattr(window, "_agent_generation", None) != getattr(window, "_briefing_user_generation158", None)
            or getattr(window, "_voice_mode", "") == "command"
            or robot.state() in {"listening", "sleeping", "error", "speaking"}):
        window._briefing_robot_owned159 = False
        return
    if finished:
        if getattr(window, "_briefing_robot_owned159", False):
            robot.set_reply_activity(False)
            robot.set_state("idle")
        window._briefing_robot_owned159 = False
        return
    window._briefing_robot_owned159 = True
    if state == 'ready':
        document = getattr(window, '_briefing_document158', None)
        waiting = any(item[1] == 'waiting' for item in getattr(document, 'sections', {}).values())
        robot.show_reply_chunk(waiting=waiting)
    else:
        robot.set_state('thinking')

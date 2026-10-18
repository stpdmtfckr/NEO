"""Bounded NVIDIA facts from existing observations, without new OS probes.

No process IDs, paths, argv, UI/user text, hotkeys or exception text are logged.
Process presence is not functional overlay readiness. Application version is
unknown until a real product-version source is supplied; MCP discovery protocol
versions must never populate that field.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)
_ENUMS = {
    "backend": {"nvidia_app", "unavailable"},
    "source": {"visible_uia", "custom_reader", "unavailable"},
    "launch_target": {"nvidia_app", "unavailable"},
    "reason": {"not_sampled", "not_windows", "overlay_not_foreground", "overlay_missing",
               "overlay_probe_unknown", "probe_busy", "runtime_python_unavailable",
               "probe_failed", "invalid_probe", "probe_timeout", "probe_unavailable",
               "control_tree_limit", "overlay_changed", "observed", "controls_not_exposed",
               "reader_unknown",
               "reader_failed", "invalid_observation"},
}
_OUTCOMES = {"observed", "pending", "already_on", "disabled", "cancelled", "timeout",
             "verified_on", "unverified", "input_failed", "invalid", "unchanged",
             "already_running", "launch_requested", "probe_unknown", "not_installed",
             "launch_failed", "unsupported", "overlay_present"}


def facts(observation=None):
    source = observation if type(observation) is dict else {}
    row = {}
    for key, allowed in _ENUMS.items():
        value = source.get(key)
        row[key] = value if type(value) is str and value in allowed else ("invalid_observation" if key == "reason" else "unavailable")
    for key in ("instant_replay", "overlay_process_present", "app_process_present"):
        value = source.get(key)
        row[key] = value if type(value) is bool else None
    row.update(application_version=None, application_version_source="unavailable",
               overlay_readiness="state_observed" if row["instant_replay"] is not None else "unverified")
    return row


def settings(config):
    """Separate configured options from gaming-gated automatic behavior."""
    row = {}
    for field, key, default in (
        ("gaming_enabled", "gaming.enabled", True),
        ("configured_auto_launch", "gaming.instant_replay.auto_launch_app", False),
        ("configured_auto_enable", "gaming.instant_replay.auto_enable", False),
    ):
        try:
            row[field] = bool(config.get(key, default))
        except Exception:
            row[field] = None
    for field in ("auto_launch", "auto_enable"):
        values = (row["gaming_enabled"], row["configured_" + field])
        row[field] = False if False in values else None if None in values else True
    return row


def emit(event, outcome, observation, *, auto_launch=None, auto_enable=None, gaming_enabled=None,
         configured_auto_launch=None, configured_auto_enable=None, pressed=None):
    """Logging failure cannot change launch, cancellation, input or notices."""
    try:
        if type(event) is not str or event not in {"game_start", "check", "launch", "terminal"}:
            return
        row = facts(observation)
        row.update(event=event, outcome=outcome if type(outcome) is str and outcome in _OUTCOMES else "unverified",
                   auto_launch=auto_launch if type(auto_launch) is bool else None,
                   auto_enable=auto_enable if type(auto_enable) is bool else None,
                   gaming_enabled=gaming_enabled if type(gaming_enabled) is bool else None,
                   configured_auto_launch=configured_auto_launch if type(configured_auto_launch) is bool else None,
                   configured_auto_enable=configured_auto_enable if type(configured_auto_enable) is bool else None,
                   pressed=pressed if type(pressed) is bool else None)
        logger.info("NEO NVIDIA diagnostic: %s", json.dumps(row, sort_keys=True, separators=(",", ":")))
    except Exception:
        return

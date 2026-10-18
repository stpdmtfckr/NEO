"""Bind a scenario request to the saved definition reviewed for its risk."""
from copy import deepcopy
from jarvis.runtime.scenarios import find_scenario, scenario_fingerprint

def bind_scenario_arguments(config, arguments):
    if arguments.get('action') != 'run' or not (arguments.get('id') or arguments.get('name')):
        return arguments
    if arguments.get('fingerprint'):
        if 'inputs' in arguments and not arguments.get('inputs_fingerprint'):
            # Values completed after the template was pinned (one missing input was asked).
            from jarvis.runtime import scenario_inputs188
            chosen = deepcopy(find_scenario(config, str(arguments.get('id') or arguments.get('name') or '')))
            resolution = scenario_inputs188.resolve(chosen, arguments.get('inputs'))
            if scenario_inputs188.has_inputs(chosen) and resolution.complete:
                arguments['inputs'] = dict(resolution.values)
                arguments['inputs_fingerprint'] = scenario_inputs188.inputs_fingerprint(arguments['fingerprint'], resolution.values)
        return arguments
    chosen = deepcopy(find_scenario(config, str(arguments.get('id') or arguments.get('name') or '')))
    if not arguments.get('id'):
        # Pin the resolved owner once; normalized input must not later choose
        # a different exact-name scenario at risk/confirmation/execution.
        if chosen.get('id'):
            arguments['id'] = str(chosen['id'])
        arguments['name'] = str(chosen.get('name', arguments.get('name', '')))
    arguments['fingerprint'] = scenario_fingerprint(chosen)
    from jarvis.runtime import scenario_inputs188
    if scenario_inputs188.has_inputs(chosen):
        # NEO 188 NEO-SCENARIO-001: approval covers the template revision AND this run's values.
        resolution = scenario_inputs188.resolve(chosen, arguments.get('inputs'))
        if resolution.complete:
            arguments['inputs'] = dict(resolution.values)
            arguments['inputs_fingerprint'] = scenario_inputs188.inputs_fingerprint(arguments['fingerprint'], resolution.values)
    return arguments


def scenario_preview(raw):
    """Plain, complete review text; never evaluate markup or hidden bidi controls."""
    import json
    from jarvis.runtime.scenario_schema import failure_policy, step_kind
    value = deepcopy(raw)
    value.setdefault('enabled', True)
    value.setdefault('max_seconds', 300)
    value.setdefault('on_failure', 'continue')  # explicit legacy default
    rows = []
    for item in value.get('steps', []):
        if not isinstance(item, dict):
            rows.append(item)
            continue
        step = deepcopy(item)
        step.setdefault('type', step_kind(step))
        step.setdefault('enabled', True)
        step['effective_on_failure'] = failure_policy(raw, step)
        rows.append(step)
    value['steps'] = rows
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)
    # Visual direction/zero-width formatting must not conceal an executable or suffix.
    hidden = set(range(0x202a, 0x202f)) | set(range(0x2066, 0x206a)) | {0x200b,0x200c,0x200d,0x200e,0x200f,0x2060,0xfeff}
    return ''.join('\\u%04x' % ord(c) if ord(c) in hidden else c for c in text)

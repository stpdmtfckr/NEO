"""Static AST catalogue only. Does not import any project module."""
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILES = [
    'jarvis/tools/implementations.py', 'jarvis/tools/extended.py',
    'jarvis/tools/integrations.py', 'jarvis/tools/domains/audio.py',
    'jarvis/tools/domains/browser_media.py', 'jarvis/tools/domains/support.py',
    'jarvis/tools/domains/system_interaction.py',
    'jarvis/integrations/local_translation.py',
    'jarvis/tools/obs_tool184.py',
]
REGISTRAR_SOURCES = {
    'jarvis/tools/implementations.py': 'core',
    'jarvis/tools/extended.py': 'extended',
    'jarvis/tools/integrations.py': 'integrations',
    'jarvis/tools/domains/audio.py': 'core',
    'jarvis/tools/domains/browser_media.py': 'core',
    'jarvis/tools/domains/support.py': 'core',
    'jarvis/tools/domains/system_interaction.py': 'core',
    'jarvis/integrations/local_translation.py': 'translation',
    'jarvis/tools/obs_tool184.py': 'integrations',
}

# Imported schema constants have an explicit, literal-only source allowlist.
# Inspect their AST; never import a registrar or execute application code here.
SCHEMA_CONSTANTS = {}
obs_tree = ast.parse((ROOT / 'jarvis/runtime/streaming_commands184.py').read_text(encoding='utf-8'))
for node in obs_tree.body:
    if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'ACTIONS' for target in node.targets):
        SCHEMA_CONSTANTS[('jarvis/tools/obs_tool184.py', 'ACTIONS')] = ast.literal_eval(node.value)


def resolved_literal(value):
    """Only reconstructed builtin values; never treat a failure marker as data."""
    if type(value) is dict:
        return 'static_unresolved' not in value and all(type(key) is str and resolved_literal(item) for key, item in value.items())
    if type(value) in (list, tuple):
        return all(resolved_literal(item) for item in value)
    return value is None or type(value) in (str, int, float, bool)

owner_tree = ast.parse((ROOT / 'jarvis/plugins/builtin/__init__.py').read_text())
owners = {}
for n in ast.walk(owner_tree):
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'BuiltinPlugin':
        for tool in ast.literal_eval(n.args[2]):
            owners[(ast.literal_eval(n.args[3]), tool)] = ast.literal_eval(n.args[0])


def effective_owner(source, tool):
    """Mirror the source-specific PluginDispatchRegistry map, not declarations alone."""
    return owners.get((source, tool), 'core.unclassified')

rows = []
for rel in FILES:
    source = (ROOT / rel).read_text(encoding='utf-8')
    tree = ast.parse(source)
    parent = {}
    for n in ast.walk(tree):
        for child in ast.iter_child_nodes(n):
            parent[child] = n

    def chain(node):
        scopes = []
        while node is not None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                scopes.append(node)
            node = parent.get(node)
        return scopes

    assignments = {}
    functions = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(n.name, []).append(n)
        if isinstance(n, ast.Assign):
            for target in n.targets:
                if isinstance(target, ast.Name):
                    assignments.setdefault(target.id, []).append((n.lineno, chain(n)[0], n.value))
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.value:
            assignments.setdefault(n.target.id, []).append((n.lineno, chain(n)[0], n.value))

    def evaluate(node, context, seen=()):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.List, ast.Tuple)):
            return [evaluate(x, context, seen) for x in node.elts]
        if isinstance(node, ast.Dict):
            result = {}
            for key, value in zip(node.keys, node.values):
                if key is None:
                    return {'static_unresolved': ast.unparse(node)}
                key = evaluate(key, context, seen)
                if type(key) is not str:
                    return {'static_unresolved': ast.unparse(node)}
                result[key] = evaluate(value, context, seen)
            return result
        if isinstance(node, ast.JoinedStr):
            parts = []
            for part in node.values:
                if isinstance(part, ast.FormattedValue):
                    if part.conversion != -1 or part.format_spec is not None:
                        return {'static_unresolved': ast.unparse(node)}
                    value = evaluate(part.value, context, seen)
                else:
                    value = evaluate(part, context, seen)
                if type(value) is not str:
                    return {'static_unresolved': ast.unparse(node)}
                parts.append(value)
            return ''.join(parts)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -evaluate(node.operand, context, seen)
        if isinstance(node, ast.Name) and node.id not in seen:
            for scope in chain(context):
                candidates = [a for a in assignments.get(node.id, []) if a[0] < context.lineno and a[1] is scope]
                if candidates:
                    _,_,value = max(candidates, key=lambda a:a[0])
                    return evaluate(value, value, (*seen,node.id))
            if (rel, node.id) in SCHEMA_CONSTANTS:
                return SCHEMA_CONSTANTS[(rel, node.id)]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            args = [evaluate(a, context, seen) for a in node.args]
            if node.func.id == '_enum':
                return {'type':'string','enum':args[0], **({'description':args[1]} if len(args)>1 else {})}
            if node.func.id in {'_schema','_object_schema'}:
                return {'type':'object','properties':args[0],'required':args[1] if len(args)>1 else [],'additionalProperties':False}
            if node.func.id == 'list' and len(args) == 1 and isinstance(args[0], (list, tuple)):
                return list(args[0])
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'join' and len(node.args) == 1 and not node.keywords):
            separator = evaluate(node.func.value, context, seen)
            items = evaluate(node.args[0], context, seen)
            # Python joins dict keys in insertion order. Only a fully static
            # literal mapping is accepted, then normalized to the same list gate.
            if type(items) is dict and resolved_literal(items):
                items = list(items)
            if type(separator) is str and type(items) in (list, tuple) and all(type(item) is str for item in items):
                return separator.join(items)
        return {'static_unresolved':ast.unparse(node)}

    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'ToolDefinition'):
            continue
        args = n.args
        if len(args)<5:
            rows.append({'source':rel,'line':n.lineno,'unresolved':ast.unparse(n)})
            continue
        name = evaluate(args[0], n)
        schema = evaluate(args[2], n)
        handler = ast.unparse(args[3])
        defs = [x for x in functions.get(handler, []) if x.lineno<n.lineno]
        fn = max(defs, key=lambda x:x.lineno) if defs else None
        actions = schema.get('properties',{}).get('action',{}).get('enum',[]) if isinstance(schema,dict) else []
        rows.append({
            'tool':name,'plugin_owner':effective_owner(REGISTRAR_SOURCES[rel], name),
            'source':rel,'registration_line':n.lineno,
            'handler':handler,'handler_line':fn.lineno if fn else None,
            'handler_end_line':fn.end_lineno if fn else None,
            'risk_resolver':ast.unparse(args[4]),
            'actions':actions, 'schema':schema,
            'description':evaluate(args[1],n),
            'reachability':'builtin registration reachable; action routing/end-to-end execution not verified',
        })

# The optional desktop registrar has one shared constructor and literal calls.
feature_path = ROOT / "jarvis/tools/desktop_features.py"
feature_tree = ast.parse(feature_path.read_text(encoding="utf-8"))
primitive = {"s":{"type":"string"}, "b":{"type":"boolean"}, "i":{"type":"integer"}}
def feature_value(node):
    if isinstance(node, ast.Name) and node.id in primitive:
        return primitive[node.id]
    if isinstance(node, ast.Dict):
        return {ast.literal_eval(k):feature_value(v) for k,v in zip(node.keys,node.values)}
    if isinstance(node, (ast.List, ast.Tuple)):
        return [feature_value(x) for x in node.elts]
    return ast.literal_eval(node)
for node in ast.walk(feature_tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "register" and len(node.args) == 5:
        name, actions = feature_value(node.args[0]), feature_value(node.args[1])
        properties = {"action":{"type":"string","enum":actions}, **feature_value(node.args[3])}
        rows.append({"tool":name, "plugin_owner":effective_owner('desktop', name), "source":"jarvis/tools/desktop_features.py",
                     "actions":actions, "description":name, "schema":{"type":"object","properties":properties,"required":["action"],"additionalProperties":False},
                     "risk_resolver":ast.unparse(node.args[4]) if len(node.args)==5 else ast.unparse(node.args[-1]),
                     "reachability":"declared registry contract; Windows end-to-end validation required"})
rows.sort(key=lambda r: r.get('tool',''))
output = {
    'method':'static AST only; no project imports or execution; schema helper constructors reconstructed, not evaluated',
    'scope':'all ToolDefinition calls in nine builtin registrar/domain modules, plus the desktop registrar',
    'tool_count':len(rows),
    'enumerated_action_count':sum(len(r.get('actions',[])) for r in rows),
    'tools':rows,
}
target = ROOT / "tests" / "fixtures" / "command_catalogue.json"
target.parent.mkdir(parents=True,exist_ok=True)
target.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"tool_count":output["tool_count"],"enumerated_action_count":output["enumerated_action_count"],"output":str(target)}))

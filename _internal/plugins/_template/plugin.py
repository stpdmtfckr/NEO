"""NEO plugin template (folders starting with «_» are never loaded).

Copy this folder to plugins/<your_name>/, edit manifest.json (name, tools,
permissions, phrases in all 7 languages) and plugin.py, then check it:
    python tools/plugin_check.py plugins/<your_name>
and say «обнови список плагинов» / «reload plugins», then «включи плагин <name>».

``context`` offers only what the approved permissions allow:
context.register(tool), context.execute(tool, args) [tools:<name>],
context.read_text(path) [files.read], context.write_text(path, text) [files.write],
context.http_get(url) [network], context.clipboard_text() [clipboard.read],
context.set_clipboard_text(text) [clipboard.write], context.notify(text) [notifications],
context.config.get(key) / context.config.own(key) / context.config.set_own(key, value) [settings].
"""
from jarvis.security import Risk
from jarvis.tools.registry import ToolDefinition, ToolExecution


def register_plugin(context) -> None:
    def handler(arguments):
        value = str(arguments.get("text", "") or "")
        return ToolExecution(True, value, {"text": value})

    context.register(ToolDefinition(
        "my_plugin",
        "Template tool: repeats the given text.",
        {"type": "object", "properties": {"action": {"type": "string", "enum": ["echo"]}, "text": {"type": "string"}},
         "required": ["action"], "additionalProperties": False},
        handler,
        lambda _args: Risk.SAFE,
    ))

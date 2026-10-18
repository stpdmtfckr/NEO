# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
__version__ = "4.0.20"

try:  # NEO 171: under the NEO process host, keep sys._base_executable pointing at the real python.exe
    from jarvis.runtime.process_host171 import fix_base_executable as _fix_base_executable
    _fix_base_executable()
except Exception:  # pragma: no cover - never block start-up
    pass

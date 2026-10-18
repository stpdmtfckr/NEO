# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
import os
import warnings

modules = [
    "PySide6",
    "requests",
    "psutil",
    "numpy",
    "sounddevice",
    "faster_whisper",
    "mss",
    "PIL",
    "openwakeword",
    "discord",
    "nacl",
    "davey",
    "imageio_ffmpeg",
]
if os.name == "nt":
    modules.extend(["pywinauto", "winsdk", "pycaw"])

for name in modules:
    # pywinauto may emit a benign UserWarning while it normalizes COM threading
    # to STA during import. The dependency probe is interested in import failures,
    # not warnings, so keep this one known warning out of the bootstrap stderr.
    if name == "pywinauto":
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"Revert to STA COM threading mode",
                category=UserWarning,
            )
            __import__(name)
    else:
        __import__(name)
print("Core Python dependencies: OK")

# HOTFIX42.1: fail early if pip somehow leaves an incompatible voice stack.
from importlib.metadata import version
from packaging.version import Version

discord_v = Version(version("discord.py"))
pynacl_v = Version(version("PyNaCl"))
if discord_v != Version("2.7.1"):
    raise RuntimeError(f"discord.py voice contract mismatch: expected 2.7.1, got {discord_v}")
if not (Version("1.5") <= pynacl_v < Version("1.6")):
    raise RuntimeError(f"PyNaCl voice contract mismatch: expected >=1.5,<1.6, got {pynacl_v}")
print(f"Discord voice dependencies: discord.py={discord_v}, PyNaCl={pynacl_v}: OK")

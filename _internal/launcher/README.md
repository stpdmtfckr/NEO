# NEO GUI launcher

`NEO.exe` is a small source-built Windows x64 GUI shim. It locates the package
with `GetModuleFileNameW`, launches the current `_internal/launch.ps1` through
the absolute system PowerShell path and waits for its existing UI-readiness
handshake. It does not contain application code, downloaded code, or an old
packaged Python runtime. Paths with spaces and Unicode use Windows wide APIs.

The primary interactive entry remains `NEO.cmd`. Start Menu/taskbar and the
existing remote-wake companion use `NEO.exe`, because Windows CreateProcess
cannot execute a CMD script directly.

Build with Zig 0.16.0 (for example the development-only `ziglang==0.16.0` package):

```text
python _internal/launcher/build_launcher.py
```

The script embeds the supplied icon and NEO version resource, verifies AMD64,
PE32+ and GUI subsystem, and writes exact source/binary SHA-256 values to
`build_manifest.json`. A compiler is not required on a user's computer.

Startup output goes to `%LOCALAPPDATA%/NeoJarvis/bootstrap/gui-launcher.log`.
Errors show a visible NEO message and advise `NEO.cmd --doctor`. Successful
startup leaves no console. No Windows security/download-origin metadata is
removed. The binary is not code-signed. Compilation and PE/import inspection
were performed on Linux; clean Windows execution remains a target-host test.

#!/usr/bin/env bash
# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
set -euo pipefail
cd "$(dirname "$0")"
for dll in kernel32 ole32 crypt32 advapi32 secur32; do
  lld-link /lib /machine:x64 "/def:${dll}.def" "/out:${dll}.lib"
done
clang --target=x86_64-pc-windows-msvc -std=c11 -O2 -Wall -Wextra -ffreestanding -fno-stack-protector -fshort-wchar -c NeoJarvisVoiceUnlockProvider.c -o NeoJarvisVoiceUnlockProvider.obj
lld-link /dll /machine:x64 /subsystem:windows /entry:DllMain /nodefaultlib \
  /out:NeoJarvisVoiceUnlockProvider.dll NeoJarvisVoiceUnlockProvider.obj \
  kernel32.lib ole32.lib crypt32.lib advapi32.lib secur32.lib \
  /export:DllGetClassObject /export:DllCanUnloadNow

rm -f NeoJarvisVoiceUnlockProvider.obj NeoJarvisVoiceUnlockProvider.lib kernel32.lib ole32.lib crypt32.lib advapi32.lib secur32.lib

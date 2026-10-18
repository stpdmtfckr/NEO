# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
import platform
import struct
import sys

bits = struct.calcsize("P") * 8
version = sys.version_info[:2]
ok = version == (3, 12) and bits == 64
print("Python {} ({}; {}-bit)".format(sys.version.split()[0], platform.machine(), bits))
if not ok:
    print("NEO requires the managed Python 3.12 x64 runtime.")
raise SystemExit(0 if ok else 1)

# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
import sounddevice as sd

devices = []
for dev in sd.query_devices():
    try:
        if int(dev.get("max_input_channels", 0)) > 0:
            devices.append(str(dev.get("name", "Unknown input device")))
    except Exception:
        pass

if devices:
    print("Input devices:")
    for index, name in enumerate(devices, 1):
        print("  {}. {}".format(index, name))
else:
    print("Input devices: NONE DETECTED")

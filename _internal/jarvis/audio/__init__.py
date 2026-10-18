# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
"""Audio services with native Windows Core Audio fast paths."""

from jarvis.audio.input_controller import AudioInputController, AudioInputDevice
from jarvis.audio.output_controller import AudioOutputController, AudioOutputDevice

__all__ = ["AudioInputController", "AudioInputDevice", "AudioOutputController", "AudioOutputDevice"]

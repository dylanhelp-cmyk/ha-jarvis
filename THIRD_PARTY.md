# Required source and dependency notices

`pywebrtc-audio==0.2.0` supplies native WebRTC AEC3. Its Apache-2.0 wrapper bundles WebRTC and other dependency notices; preserve the wheel's LICENSE and NOTICE files. Sources: https://pypi.org/project/pywebrtc-audio/0.2.0/ and https://github.com/strands-labs/pywebrtc-audio/blob/v0.2.0/LICENSE . Acoustic echo handling remains unverified until integrated and tested in the room.

`ha_jarvis/firmware/voice-pe-hardware.yaml` derives from ESPHome's Home Assistant Voice PE configuration at commit `d4e6fa43d6a1a7d342b7d4d403567e06516719de`. Its MIT notice is reproduced in `ha_jarvis/firmware/UPSTREAM-LICENSE.txt`, in the App image and the public firmware source repository.

ESPHome Builder downloads the pinned official `voice_kit`, XMOS v1.3.1 image, Hey Jarvis model and Espressif WebSocket component identified in `ha_jarvis/firmware/INSTALL.md`. Their original repositories/artifacts retain their license notices. The Hey Jarvis model source is Apache-2.0; `esp_websocket_client` is Apache-2.0. No third-party precompiled firmware is redistributed in the project release ZIP.

The App installs aiohttp (Apache-2.0) and NumPy (BSD-3-Clause), with their dependency metadata/licenses retained in the Python installation. Components are used as installed libraries/processes rather than copied source.

The cloud transport installs [aiortc 1.15.0](https://pypi.org/project/aiortc/1.15.0/) and [PyAV 17.1.0](https://pypi.org/project/av/17.1.0/), both BSD-3-Clause. Their installed distributions retain their license files and dependency notices. PyAV binary wheels bundle FFmpeg libraries and their associated license notices. aiortc also uses installed SRTP, OpenSSL and ICE dependencies with their distribution metadata/notices retained. No library source is copied into Jarvis.

The App runs Debian's [eSpeak NG package](https://packages.debian.org/bookworm/espeak-ng) as a separate process for fixed synthetic test prompts. Its installed license notices remain under `/usr/share/doc/espeak-ng/copyright`.

[webrtcvad-wheels 2.0.14.post1](https://pypi.org/project/webrtcvad-wheels/2.0.14.post1/) provides the local speech activity estimate. Its wheel retains the Python wrapper and bundled WebRTC notices.

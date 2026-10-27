# Required source and dependency notices

`ha_jarvis/firmware/voice-pe-hardware.yaml` derives from ESPHome's Home Assistant Voice PE configuration at commit `d4e6fa43d6a1a7d342b7d4d403567e06516719de`. Its MIT notice is reproduced in `ha_jarvis/firmware/UPSTREAM-LICENSE.txt`, in the App image and the public firmware source repository.

ESPHome Builder downloads the pinned official `voice_kit`, XMOS v1.3.1 image, Hey Jarvis model and Espressif WebSocket component identified in `ha_jarvis/firmware/INSTALL.md`. Their original repositories/artifacts retain their license notices. The Hey Jarvis model source is Apache-2.0; `esp_websocket_client` is Apache-2.0. No third-party precompiled firmware is redistributed in the project release ZIP.

The App installs aiohttp (Apache-2.0), NumPy (BSD-3-Clause), aioslimproto (Apache-2.0) and pyatv (MIT), with their dependency metadata/licenses retained in the Python installation. ffmpeg is installed from Debian's package repository with Debian's copyright notices retained under `/usr/share/doc/`. Components are used as installed libraries/processes rather than copied source.

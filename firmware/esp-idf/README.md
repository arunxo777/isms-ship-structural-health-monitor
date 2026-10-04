# ISMS node firmware (ESP-IDF)

Primary firmware for the Waveshare ESP32-S3-Touch-LCD-3.5B. It is the official
[01_factory](https://github.com/waveshareteam/ESP32-S3-Touch-LCD-3.5B/tree/main/ESP-IDF/01_factory)
project with the demo UI replaced by the ISMS node application.

## Build and flash

Requires ESP-IDF **v5.4** or newer.

```bash
cp main/isms_secrets.example.h main/isms_secrets.h   # then edit Wi-Fi and Pi IP
idf.py set-target esp32s3                            # first time only
idf.py -p <PORT> build flash monitor                 # e.g. COM7 or /dev/ttyACM0
```

Without `isms_secrets.h` the project still builds with placeholder credentials (this is what CI
compiles), but the node will not join your network.

`sdkconfig` is committed so a fresh clone builds exactly the configuration that runs on the board.
Managed components are pinned by `dependencies.lock` and downloaded on the first build.

## Source map

| Path | Contents |
| --- | --- |
| `main/main.cpp` | boot sequence, sensor/mic/HTTP/camera FreeRTOS tasks, SPIFFS offline buffer |
| `main/isms_config.h` | periods, thresholds, calibration constants, feature switches |
| `main/isms_secrets.h` | Wi-Fi credentials and Pi address (git-ignored) |
| `main/isms_camera.c` | OV5640 JPEG QVGA setup |
| `main/isms_ui.cpp` | LVGL live dashboard |
| `components/esp_bsp` | Waveshare board support (I²C, IMU, codec, PMU, display, Wi-Fi) |

**Do not edit** `components/esp_bsp/bsp_display.c` — it holds the AXS15231B QSPI init sequence.

Feature switches in `isms_config.h`: `ISMS_HTTP_ENABLE`, `ISMS_CAMERA_ENABLE`, `ISMS_MIC_ENABLE`,
`ISMS_TOUCH_ENABLE`, `ISMS_GPS_ENABLE`. Pin details and troubleshooting are in
[docs/hardware.md](../../docs/hardware.md).

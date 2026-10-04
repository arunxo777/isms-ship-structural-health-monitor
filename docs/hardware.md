# Hardware

## Bill of materials

| Part | Role |
| --- | --- |
| [Waveshare ESP32-S3-Touch-LCD-3.5B](https://www.waveshare.com/esp32-s3-touch-lcd-3.5b.htm) | Hull node: ESP32-S3, 16 MB flash, 8 MB octal PSRAM, 3.5" QSPI LCD, QMI8658 IMU, ES8311 codec, AXP2101 PMU |
| OV5640 camera module (DVP) | Plate imaging, JPEG QVGA |
| Raspberry Pi 5 (8 GB recommended) | Processing node: API, vision, Ollama |
| DSI or HDMI display | Command-and-control kiosk |
| Wi-Fi access point or phone hotspot | Shared network for node and Pi |

## Node pin map

All on-board peripherals use the Waveshare factory BSP wiring.

| Function | Pins | Notes |
| --- | --- | --- |
| I²C (IMU, PMU, codec, camera SCCB) | SDA GPIO8, SCL GPIO7 @ 400 kHz | shared bus |
| QMI8658 IMU | addr 0x6B | ±4 g, ±512 dps |
| AXP2101 PMU | addr 0x34 | |
| ES8311 I²S | MCLK 44, BCLK 13, LRCK 15, DOUT 16, DIN 14 | 16 kHz mono capture |
| AXS15231B QSPI LCD | CS 12, SCLK 5, D0–D3 1–4, BL 6 | do not modify `bsp_display.c` |
| Camera DVP | Y2–Y9 45, 47, 48, 46, 42, 40, 39, 21 · PCLK 41 · VSYNC 17 · HREF 18 · XCLK 38 | |
| GPS UART2 (optional) | TX 43, RX 44, 9600 baud | disabled by default |

### Pin conflicts to know about

- **GPIO44** is both ES8311 MCLK and the GPS RX pin from the original spec. GPS is disabled
  (`ISMS_GPS_ENABLE 0`) so audio works; move GPS to a free GPIO that is not on the camera bus.
- **GPIO42** is camera Y6 — do not use it for GPS.
- **GPIO40/41** belong to the camera. An HC-SR04 originally on these pins was removed.
- The generic I²S map (BCLK 15, WS 16, DIN 17) does **not** apply to this board.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `AXP2101: Init PMU FAILED`, I²C reads hang, task watchdog in `bsp_qmi8658_reg_read` | bus left busy after a warm reset — unplug USB fully and power-cycle |
| Wi-Fi `reason 5` (`ASSOC_TOOMANY`) | the hotspot is at its client limit — raise it or disconnect a device |
| Wi-Fi associates then drops (`reason 15`/`204`) | wrong password, or a WPA3-only network (the firmware supports WPA2/WPA3 transition with PMF) |
| HMI shows `DARK` | lens covered or no light on the plate |

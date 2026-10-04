# ISMS node — Arduino / PlatformIO port

The same application logic as [`../esp-idf`](../esp-idf) (sensors, FFT, GPS zones, HTTP,
SPIFFS backup) on the Arduino framework. Prefer the ESP-IDF firmware on the Waveshare 3.5B: it
reuses the vendor BSP for the display, IMU, codec and PMU, and it includes the camera pipeline.

```bash
cp src/secrets.example.h src/secrets.h   # then edit Wi-Fi and Pi IP
pio run -t upload -t monitor
```

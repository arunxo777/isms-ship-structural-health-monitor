/*
 * ISMS node compile-time configuration.
 * Matches the hackathon hardware spec and the Pi .env defaults.
 *
 * Onboard Waveshare ESP32-S3-Touch-LCD-3.5B pins come from the
 * factory BSP (see components/esp_bsp). Do not change LCD QSPI pins.
 */

#pragma once

/* Wi-Fi credentials and the Pi address live in the git-ignored
 * isms_secrets.h (template: isms_secrets.example.h). Without it the build
 * still succeeds with placeholders, which is what CI compiles. */
#if __has_include("isms_secrets.h")
#include "isms_secrets.h"
#endif

#ifndef ISMS_WIFI_SSID
#define ISMS_WIFI_SSID          "your-wifi-ssid"
#endif
#ifndef ISMS_WIFI_PASSWORD
#define ISMS_WIFI_PASSWORD      "your-wifi-password"
#endif

/* The Raspberry Pi 5 is the processing node: it runs the vision stage, the
 * fusion engine, Gemma 4 and the CIC display. Point the node at the Pi, not
 * at the laptop. */
#ifndef ISMS_PI_HOST
#define ISMS_PI_HOST            "192.168.1.100"
#endif
#define ISMS_PI_PORT            8000
#define ISMS_POST_PATH          "/sensor-data"
#define ISMS_VERDICT_PATH       "/latest-verdict"
#define ISMS_CAMERA_PATH        "/camera-frame"
#define ISMS_HTTP_TIMEOUT_MS    3000
/* Keep camera uploads short so they cannot starve /sensor-data. */
#define ISMS_CAM_TIMEOUT_MS     4000
/* 2 s camera — enough samples for fast rust confirm (2 of 3). */
#define ISMS_CAM_PERIOD_MS      2000

#define ISMS_HTTP_ENABLE        1
#define ISMS_CAMERA_ENABLE      1
#define ISMS_MIC_ENABLE         1
#define ISMS_TOUCH_ENABLE       0

#define ISMS_GPS_ENABLE         0
#define ISMS_GPS_UART           UART_NUM_2
#define ISMS_GPS_TX_GPIO        GPIO_NUM_43
#define ISMS_GPS_RX_GPIO        GPIO_NUM_44
#define ISMS_GPS_BAUD           9600

#define ISMS_ACC_SENS_G         (4.0f / 32768.0f)
#define ISMS_GYR_SENS_DPS       (512.0f / 32768.0f)
#define ISMS_GRAVITY            9.80665f

#define ISMS_IMU_PERIOD_MS      50
#define ISMS_RMS_WINDOW         600
#define ISMS_IMU_CAL_SAMPLES    60
#define ISMS_LPF_ALPHA          0.18f
#define ISMS_ACC_DEADBAND_G     0.02f
#define ISMS_GYR_DEADBAND_DPS   1.5f
#define ISMS_ANG_DEADBAND_DEG   1.5f
#define ISMS_ACC_QUANT_G        0.01f
#define ISMS_GYR_QUANT_DPS      0.1f
#define ISMS_ANG_QUANT_DEG      0.1f
#define ISMS_STILL_GYR_DPS      8.0f
#define ISMS_STILL_ACC_ERR_G    0.10f
#define ISMS_STILL_ENTER        12
#define ISMS_STILL_EXIT         2
#define ISMS_CAL_QUIET_GYR_DPS  20.0f
#define ISMS_MIC_DEADBAND       0.002f
#define ISMS_MIC_QUANT          0.001f
#define ISMS_MIC_CAL_SAMPLES    8
#define ISMS_MIC_PERIOD_MS      80

#define ISMS_SLOW_PERIOD_MS     500
#define ISMS_FFT_N              512
#define ISMS_AUDIO_FS           16000
#define ISMS_AUDIO_GAIN_DB      50.0f

#define ISMS_BACKUP_MAX         10
#define ISMS_BACKUP_PATH        "/spiffs/backup.jsonl"

#define ISMS_ZONE_A_LAT_MIN     9.90
#define ISMS_ZONE_A_LAT_MAX     9.95
#define ISMS_ZONE_A_LON_MIN     78.10
#define ISMS_ZONE_A_LON_MAX     78.15
#define ISMS_ZONE_B_LAT_MIN     9.95
#define ISMS_ZONE_B_LAT_MAX     10.00
#define ISMS_ZONE_B_LON_MIN     78.15
#define ISMS_ZONE_B_LON_MAX     78.20

#define ISMS_LCD_H_RES          320
#define ISMS_LCD_V_RES          480
#define ISMS_LCD_BUFFER_SIZE    (ISMS_LCD_H_RES * ISMS_LCD_V_RES)

/*
 * ISMS sensor node — ESP32-S3-Touch-LCD-3.5B
 *
 * Built on the Waveshare factory ESP-IDF project so the AXS15231B
 * QSPI display, TCA9554 reset expander, AXP2101 rails, QMI8658 and
 * ES8311 keep their proven BSP bring-up. Display driver code is not
 * modified.
 *
 * FreeRTOS layout:
 *   Core 0 / prio 3  sensor_task     IMU 50 ms, mic/GPS 500 ms
 *   Core 1 / prio 2  http_post_task  POST /sensor-data every 500 ms
 *   Core 1 / prio 1  verdict_task    GET /latest-verdict + LVGL refresh
 *   Core 1 / prio 1  camera_task     JPEG POST /camera-frame every 2 s
 *   HTTP serialized via g_http_mux — sensor waits, camera/verdict skip if busy.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>
#include <stdint.h>
#include <sys/stat.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

#include "nvs_flash.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_check.h"
#include "esp_timer.h"
#include "esp_http_client.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_sntp.h"
#include "esp_spiffs.h"
#include "esp_heap_caps.h"
#include "esp_io_expander_tca9554.h"
#include "cJSON.h"

#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp_rom_sys.h"

#include "bsp_i2c.h"
#include "bsp_qmi8658.h"
#include "bsp_pcf85063.h"
#include "bsp_display.h"
#include "bsp_touch.h"
#include "bsp_wifi.h"
#include "bsp_es8311.h"
#include "bsp_axp2101.h"

#include "lv_port.h"
#include "isms_config.h"
#include "isms_ui.h"
#include "isms_camera.h"
#include "esp_camera.h"
#include <assert.h>

static const char *TAG = "ISMS";

typedef struct {
    char timestamp[32];
    char zone_id[16];
    double lat;
    double lon;
    bool gps_valid;
    float accel_x;
    float accel_y;
    float accel_z;
    float gyro_x;
    float gyro_y;
    float gyro_z;
    int16_t acc_raw_x;
    int16_t acc_raw_y;
    int16_t acc_raw_z;
    int16_t gyr_raw_x;
    int16_t gyr_raw_y;
    int16_t gyr_raw_z;
    float angle_x;
    float angle_y;
    float angle_z;
    float acoustic_rms;
    float acoustic_peak_freq;
    float crack_score;
    bool crack_detected;
    int crack_count;
    float accel_rms_30s;
    bool wifi_ok;
    bool imu_ok;
    bool imu_calibrated;
    bool still;
    int cal_pct;
    bool mic_ok;
} isms_reading_t;

typedef struct {
    char status[16];
    char fault_type[96];
    char time_to_fault[48];
    float crack_score;
    bool crack_detected;
    bool valid;
} isms_verdict_t;

static isms_reading_t g_reading;
static isms_verdict_t g_verdict;
static SemaphoreHandle_t g_data_mux;
/* One radio owner at a time — sensor / camera / verdict must not open
 * concurrent esp_http_client sessions on the same STA interface. */
static SemaphoreHandle_t g_http_mux;
static i2c_master_bus_handle_t g_i2c_bus;

static float g_rms_buf[ISMS_RMS_WINDOW];
static int g_rms_head;
static int g_rms_count;
static double g_rms_sum_sq;
static int g_accel_cal_count;
static double g_accel_cal_sum_x;
static double g_accel_cal_sum_y;
static double g_accel_cal_sum_z;
static double g_gyro_cal_sum_x;
static double g_gyro_cal_sum_y;
static double g_gyro_cal_sum_z;
static float g_accel_zero_x;
static float g_accel_zero_y;
static float g_accel_zero_z;
static float g_gyro_bias_x;
static float g_gyro_bias_y;
static float g_gyro_bias_z;
static float g_acc_scale = 1.0f;
static float g_f_ax, g_f_ay, g_f_az, g_f_gx, g_f_gy, g_f_gz;
static float g_s_ax, g_s_ay, g_s_az, g_s_gx, g_s_gy, g_s_gz;
static float g_s_angx, g_s_angy, g_s_angz;
static float g_yaw;
static bool g_lpf_ready;
static volatile float g_mic_rms;
static volatile float g_mic_peak;
static volatile bool g_mic_ok;
static volatile bool g_mic_calibrated;
static bool g_still;
static bool g_was_still;
static int g_still_on;
static int g_still_off;

static char g_backup[ISMS_BACKUP_MAX][768];
static int g_backup_count;

esp_io_expander_handle_t expander_handle = NULL;
esp_lcd_panel_io_handle_t io_handle = NULL;
esp_lcd_panel_handle_t panel_handle = NULL;
static lv_disp_t *lvgl_disp;
#if ISMS_TOUCH_ENABLE
static lv_indev_t *lvgl_touch_indev = NULL;
#endif

/* -------------------------------------------------------------------------- */
/* Validation helpers                                                          */
/* -------------------------------------------------------------------------- */

static bool isms_finite(float v)
{
    return isfinite((double)v) != 0;
}

static float clampf(float v, float lo, float hi)
{
    if (!isms_finite(v)) {
        return lo;
    }
    if (v < lo) {
        return lo;
    }
    if (v > hi) {
        return hi;
    }
    return v;
}

static bool wifi_is_connected(void)
{
    wifi_ap_record_t ap;
    return esp_wifi_sta_get_ap_info(&ap) == ESP_OK;
}

static bool wifi_has_ip(void)
{
    esp_netif_t *netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    if (netif == NULL) {
        return false;
    }
    esp_netif_ip_info_t ip;
    if (esp_netif_get_ip_info(netif, &ip) != ESP_OK) {
        return false;
    }
    return ip.ip.addr != 0;
}

/* -------------------------------------------------------------------------- */
/* Timestamp / zone                                                            */
/* -------------------------------------------------------------------------- */

static void format_timestamp(char *buf, size_t n)
{
    time_t now = time(NULL);
    struct tm tmv;
    gmtime_r(&now, &tmv);
    if (tmv.tm_year + 1900 < 2025) {
        int64_t us = esp_timer_get_time();
        int sec = (int)((us / 1000000) % 86400);
        snprintf(buf, n, "2026-09-09T%02d:%02d:%02d",
                 sec / 3600, (sec / 60) % 60, sec % 60);
        return;
    }
    strftime(buf, n, "%Y-%m-%dT%H:%M:%S", &tmv);
}

static void assign_zone(double lat, double lon, bool valid, char *zone, size_t n)
{
    if (!valid || !isfinite(lat) || !isfinite(lon)) {
        strncpy(zone, "ZONE_UNKNOWN", n - 1);
        zone[n - 1] = '\0';
        return;
    }
    if (lat >= ISMS_ZONE_A_LAT_MIN && lat <= ISMS_ZONE_A_LAT_MAX &&
        lon >= ISMS_ZONE_A_LON_MIN && lon <= ISMS_ZONE_A_LON_MAX) {
        strncpy(zone, "ZONE_A", n - 1);
    } else if (lat >= ISMS_ZONE_B_LAT_MIN && lat <= ISMS_ZONE_B_LAT_MAX &&
               lon >= ISMS_ZONE_B_LON_MIN && lon <= ISMS_ZONE_B_LON_MAX) {
        strncpy(zone, "ZONE_B", n - 1);
    } else {
        strncpy(zone, "ZONE_C", n - 1);
    }
    zone[n - 1] = '\0';
}

/* -------------------------------------------------------------------------- */
/* 512-point real FFT (Cooley–Tukey radix-2) for peak-frequency extraction     */
/* -------------------------------------------------------------------------- */

static void bit_reverse(float *re, float *im, int n)
{
    int j = 0;
    for (int i = 0; i < n; i++) {
        if (i < j) {
            float tr = re[i];
            re[i] = re[j];
            re[j] = tr;
            float ti = im[i];
            im[i] = im[j];
            im[j] = ti;
        }
        int m = n >> 1;
        while (m >= 1 && j >= m) {
            j -= m;
            m >>= 1;
        }
        j += m;
    }
}

static void fft_radix2(float *re, float *im, int n)
{
    bit_reverse(re, im, n);
    for (int len = 2; len <= n; len <<= 1) {
        float ang = -2.0f * (float)M_PI / (float)len;
        float wlen_re = cosf(ang);
        float wlen_im = sinf(ang);
        for (int i = 0; i < n; i += len) {
            float w_re = 1.0f;
            float w_im = 0.0f;
            for (int j = 0; j < len / 2; j++) {
                float u_re = re[i + j];
                float u_im = im[i + j];
                float v_re = re[i + j + len / 2] * w_re - im[i + j + len / 2] * w_im;
                float v_im = re[i + j + len / 2] * w_im + im[i + j + len / 2] * w_re;
                re[i + j] = u_re + v_re;
                im[i + j] = u_im + v_im;
                re[i + j + len / 2] = u_re - v_re;
                im[i + j + len / 2] = u_im - v_im;
                float n_re = w_re * wlen_re - w_im * wlen_im;
                w_im = w_re * wlen_im + w_im * wlen_re;
                w_re = n_re;
            }
        }
    }
}

static float fft_peak_hz(const float *samples, int n, float fs)
{
    static float re[ISMS_FFT_N];
    static float im[ISMS_FFT_N];
    for (int i = 0; i < n; i++) {
        /* Hamming window reduces spectral leakage on hull noise */
        float w = 0.54f - 0.46f * cosf(2.0f * (float)M_PI * (float)i / (float)(n - 1));
        re[i] = samples[i] * w;
        im[i] = 0.0f;
    }
    fft_radix2(re, im, n);
    float best = 0.0f;
    int best_k = 1;
    for (int k = 1; k < n / 2; k++) {
        float mag = re[k] * re[k] + im[k] * im[k];
        if (mag > best) {
            best = mag;
            best_k = k;
        }
    }
    return (float)best_k * fs / (float)n;
}

/* -------------------------------------------------------------------------- */
/* NMEA (GGA / RMC) — TinyGPSPlus-equivalent subset                            */
/* -------------------------------------------------------------------------- */

static double nmea_coord(const char *field, const char *hemi)
{
    if (field == NULL || field[0] == '\0') {
        return NAN;
    }
    double raw = atof(field);
    int deg = (int)(raw / 100.0);
    double min = raw - (double)deg * 100.0;
    double dec = (double)deg + min / 60.0;
    if (hemi && (hemi[0] == 'S' || hemi[0] == 'W')) {
        dec = -dec;
    }
    return dec;
}

static void nmea_process_sentence(char *line, double *lat, double *lon, bool *valid)
{
    if (line[0] != '$') {
        return;
    }
    /* Strip checksum */
    char *star = strchr(line, '*');
    if (star) {
        *star = '\0';
    }
    char *tok[20];
    int n = 0;
    char *save = NULL;
    char *p = strtok_r(line, ",", &save);
    while (p && n < 20) {
        tok[n++] = p;
        p = strtok_r(NULL, ",", &save);
    }
    if (n < 7) {
        return;
    }
    if (strstr(tok[0], "GGA")) {
        double la = nmea_coord(tok[2], tok[3]);
        double lo = nmea_coord(tok[4], tok[5]);
        int fix = atoi(tok[6]);
        if (fix > 0 && isfinite(la) && isfinite(lo)) {
            *lat = la;
            *lon = lo;
            *valid = true;
        } else {
            *valid = false;
        }
    } else if (strstr(tok[0], "RMC") && n > 7) {
        if (tok[2][0] == 'A') {
            double la = nmea_coord(tok[3], tok[4]);
            double lo = nmea_coord(tok[5], tok[6]);
            if (isfinite(la) && isfinite(lo)) {
                *lat = la;
                *lon = lo;
                *valid = true;
            }
        } else {
            *valid = false;
        }
    }
}

static void gps_poll(double *lat, double *lon, bool *valid)
{
#if !ISMS_GPS_ENABLE
    (void)lat;
    (void)lon;
    (void)valid;
    return;
#else
    static char line[128];
    static int idx;
    uint8_t tmp[128];
    int len = uart_read_bytes(ISMS_GPS_UART, tmp, sizeof(tmp), 0);
    for (int i = 0; i < len; i++) {
        char c = (char)tmp[i];
        if (c == '\r') {
            continue;
        }
        if (c == '\n' || idx >= (int)sizeof(line) - 1) {
            line[idx] = '\0';
            if (idx > 6) {
                nmea_process_sentence(line, lat, lon, valid);
            }
            idx = 0;
            continue;
        }
        line[idx++] = c;
    }
#endif
}

/* -------------------------------------------------------------------------- */
/* IMU RMS window                                                              */
/* -------------------------------------------------------------------------- */

static void rms_push(float mag_g)
{
    if (!isms_finite(mag_g)) {
        return;
    }
    if (g_rms_count == ISMS_RMS_WINDOW) {
        float old = g_rms_buf[g_rms_head];
        g_rms_sum_sq -= (double)old * (double)old;
    } else {
        g_rms_count++;
    }
    g_rms_buf[g_rms_head] = mag_g;
    g_rms_sum_sq += (double)mag_g * (double)mag_g;
    g_rms_head = (g_rms_head + 1) % ISMS_RMS_WINDOW;
    if (g_rms_sum_sq < 0.0) {
        g_rms_sum_sq = 0.0;
    }
}

static float rms_value(void)
{
    if (g_rms_count <= 0) {
        return 0.0f;
    }
    return sqrtf((float)(g_rms_sum_sq / (double)g_rms_count));
}

static float lpf_apply(float *state, float x)
{
    if (!g_lpf_ready) {
        *state = x;
        return x;
    }
    *state = ISMS_LPF_ALPHA * x + (1.0f - ISMS_LPF_ALPHA) * (*state);
    return *state;
}

static float quantize(float x, float step)
{
    if (step <= 0.0f) {
        return x;
    }
    return roundf(x / step) * step;
}

static float hold_delta(float shown, float x, float band)
{
    return (fabsf(x - shown) < band) ? shown : x;
}

static void accel_to_rp(float ax, float ay, float az, float *roll, float *pitch)
{
    *roll = atan2f(ay, az) * (180.0f / (float)M_PI);
    float den = sqrtf(ay * ay + az * az);
    if (den < 1e-6f) {
        den = 1e-6f;
    }
    *pitch = atan2f(-ax, den) * (180.0f / (float)M_PI);
}

static void imu_snap_display(float ax, float ay, float az, float roll, float pitch, float yaw)
{
    g_s_ax = quantize(ax, ISMS_ACC_QUANT_G);
    g_s_ay = quantize(ay, ISMS_ACC_QUANT_G);
    g_s_az = quantize(az, ISMS_ACC_QUANT_G);
    g_s_gx = 0.0f;
    g_s_gy = 0.0f;
    g_s_gz = 0.0f;
    g_s_angx = quantize(roll, ISMS_ANG_QUANT_DEG);
    g_s_angy = quantize(pitch, ISMS_ANG_QUANT_DEG);
    g_s_angz = quantize(yaw, ISMS_ANG_QUANT_DEG);
}

static void imu_hold_still(float ax, float ay, float az, float yaw)
{
    float mag = sqrtf(ax * ax + ay * ay + az * az);
    if (mag > 0.30f) {
        ax /= mag;
        ay /= mag;
        az /= mag;
    }
    float roll, pitch;
    accel_to_rp(ax, ay, az, &roll, &pitch);
    imu_snap_display(ax, ay, az, roll, pitch, yaw);
}

static bool imu_sample_quiet(float ax_g, float ay_g, float az_g, float gx, float gy, float gz)
{
    static float pgx, pgy, pgz, pax, pay, paz;
    static bool have;
    float amag = sqrtf(ax_g * ax_g + ay_g * ay_g + az_g * az_g);
    if (fabsf(amag - 1.0f) > 0.50f) {
        have = false;
        return false;
    }
    if (!have) {
        pgx = gx;
        pgy = gy;
        pgz = gz;
        pax = ax_g;
        pay = ay_g;
        paz = az_g;
        have = true;
        return false;
    }
    float dg = fabsf(gx - pgx) + fabsf(gy - pgy) + fabsf(gz - pgz);
    float da = fabsf(ax_g - pax) + fabsf(ay_g - pay) + fabsf(az_g - paz);
    pgx = gx;
    pgy = gy;
    pgz = gz;
    pax = ax_g;
    pay = ay_g;
    paz = az_g;
    return (dg < 35.0f) && (da < 0.20f);
}

static bool imu_calibrate_filter(float ax_g, float ay_g, float az_g,
                                 float gx, float gy, float gz,
                                 float *dax, float *day, float *daz,
                                 float *dgx, float *dgy, float *dgz,
                                 float *dangx, float *dangy, float *dangz,
                                 bool *still, int *cal_pct)
{
    if (g_accel_cal_count < ISMS_IMU_CAL_SAMPLES) {
        static int cal_bad;
        if (!imu_sample_quiet(ax_g, ay_g, az_g, gx, gy, gz)) {
            if (++cal_bad >= 8) {
                cal_bad = 0;
                g_accel_cal_count = 0;
                g_accel_cal_sum_x = g_accel_cal_sum_y = g_accel_cal_sum_z = 0;
                g_gyro_cal_sum_x = g_gyro_cal_sum_y = g_gyro_cal_sum_z = 0;
            }
            *cal_pct = (100 * g_accel_cal_count) / ISMS_IMU_CAL_SAMPLES;
            *dax = g_s_ax;
            *day = g_s_ay;
            *daz = g_s_az;
            *dgx = 0.0f;
            *dgy = 0.0f;
            *dgz = 0.0f;
            *dangx = g_s_angx;
            *dangy = g_s_angy;
            *dangz = 0.0f;
            *still = false;
            return false;
        }
        cal_bad = 0;
        g_accel_cal_sum_x += ax_g;
        g_accel_cal_sum_y += ay_g;
        g_accel_cal_sum_z += az_g;
        g_gyro_cal_sum_x += gx;
        g_gyro_cal_sum_y += gy;
        g_gyro_cal_sum_z += gz;
        g_accel_cal_count++;
        *cal_pct = (100 * g_accel_cal_count) / ISMS_IMU_CAL_SAMPLES;

        float axf = lpf_apply(&g_f_ax, ax_g);
        float ayf = lpf_apply(&g_f_ay, ay_g);
        float azf = lpf_apply(&g_f_az, az_g);
        g_lpf_ready = true;
        if (g_accel_cal_count == 1) {
            imu_hold_still(axf, ayf, azf, 0.0f);
        }
        *dax = g_s_ax;
        *day = g_s_ay;
        *daz = g_s_az;
        *dgx = 0.0f;
        *dgy = 0.0f;
        *dgz = 0.0f;
        *dangx = g_s_angx;
        *dangy = g_s_angy;
        *dangz = 0.0f;
        *still = false;

        if (g_accel_cal_count == ISMS_IMU_CAL_SAMPLES) {
            const double n = (double)ISMS_IMU_CAL_SAMPLES;
            g_accel_zero_x = (float)(g_accel_cal_sum_x / n);
            g_accel_zero_y = (float)(g_accel_cal_sum_y / n);
            g_accel_zero_z = (float)(g_accel_cal_sum_z / n);
            g_gyro_bias_x = (float)(g_gyro_cal_sum_x / n);
            g_gyro_bias_y = (float)(g_gyro_cal_sum_y / n);
            g_gyro_bias_z = (float)(g_gyro_cal_sum_z / n);
            float mag0 = sqrtf(g_accel_zero_x * g_accel_zero_x +
                               g_accel_zero_y * g_accel_zero_y +
                               g_accel_zero_z * g_accel_zero_z);
            g_acc_scale = (mag0 > 0.30f) ? (1.0f / mag0) : 1.0f;
            g_accel_zero_x *= g_acc_scale;
            g_accel_zero_y *= g_acc_scale;
            g_accel_zero_z *= g_acc_scale;
            g_yaw = 0.0f;
            g_f_gx = 0.0f;
            g_f_gy = 0.0f;
            g_f_gz = 0.0f;
            g_still = true;
            g_was_still = true;
            g_still_on = ISMS_STILL_ENTER;
            g_still_off = 0;
            imu_hold_still(g_accel_zero_x, g_accel_zero_y, g_accel_zero_z, 0.0f);
            *dax = g_s_ax;
            *day = g_s_ay;
            *daz = g_s_az;
            *dgx = 0.0f;
            *dgy = 0.0f;
            *dgz = 0.0f;
            *dangx = g_s_angx;
            *dangy = g_s_angy;
            *dangz = g_s_angz;
            *still = true;
            *cal_pct = 100;
            ESP_LOGI(TAG,
                     "IMU calibrated  acc0=%.3f %.3f %.3f g  gyro_bias=%.2f %.2f %.2f dps  scale=%.3f",
                     g_accel_zero_x, g_accel_zero_y, g_accel_zero_z,
                     g_gyro_bias_x, g_gyro_bias_y, g_gyro_bias_z, g_acc_scale);
        }
        return false;
    }

    *cal_pct = 100;
    float axf = lpf_apply(&g_f_ax, ax_g) * g_acc_scale;
    float ayf = lpf_apply(&g_f_ay, ay_g) * g_acc_scale;
    float azf = lpf_apply(&g_f_az, az_g) * g_acc_scale;
    float gxf = lpf_apply(&g_f_gx, gx - g_gyro_bias_x);
    float gyf = lpf_apply(&g_f_gy, gy - g_gyro_bias_y);
    float gzf = lpf_apply(&g_f_gz, gz - g_gyro_bias_z);
    g_lpf_ready = true;

    float mag = sqrtf(axf * axf + ayf * ayf + azf * azf);
    float gmag = sqrtf(gxf * gxf + gyf * gyf + gzf * gzf);
    float dpose = sqrtf((axf - g_s_ax) * (axf - g_s_ax) +
                        (ayf - g_s_ay) * (ayf - g_s_ay) +
                        (azf - g_s_az) * (azf - g_s_az));
    bool raw_still = (gmag < ISMS_STILL_GYR_DPS) &&
                     (fabsf(mag - 1.0f) < ISMS_STILL_ACC_ERR_G) &&
                     (dpose < 0.08f);
    if (!raw_still) {
        g_still_off++;
        g_still_on = 0;
        if (g_still_off >= ISMS_STILL_EXIT) {
            g_still = false;
        }
    } else {
        g_still_on++;
        g_still_off = 0;
        if (g_still_on >= ISMS_STILL_ENTER) {
            g_still = true;
        }
    }

    if (fabsf(gxf) < ISMS_GYR_DEADBAND_DPS) {
        gxf = 0.0f;
    }
    if (fabsf(gyf) < ISMS_GYR_DEADBAND_DPS) {
        gyf = 0.0f;
    }
    if (fabsf(gzf) < ISMS_GYR_DEADBAND_DPS) {
        gzf = 0.0f;
    }

    float roll, pitch;
    accel_to_rp(axf, ayf, azf, &roll, &pitch);
    const float dt = (float)ISMS_IMU_PERIOD_MS / 1000.0f;
    if (!g_still) {
        g_yaw += gzf * dt;
        if (g_yaw > 180.0f) {
            g_yaw -= 360.0f;
        } else if (g_yaw < -180.0f) {
            g_yaw += 360.0f;
        }
    }

    if (g_still && !g_was_still) {
        imu_hold_still(axf, ayf, azf, g_yaw);
    }
    g_was_still = g_still;

    if (g_still) {
        *dax = g_s_ax;
        *day = g_s_ay;
        *daz = g_s_az;
        *dgx = 0.0f;
        *dgy = 0.0f;
        *dgz = 0.0f;
        *dangx = g_s_angx;
        *dangy = g_s_angy;
        *dangz = g_s_angz;
    } else {
        g_s_ax = hold_delta(g_s_ax, quantize(axf, ISMS_ACC_QUANT_G), ISMS_ACC_DEADBAND_G);
        g_s_ay = hold_delta(g_s_ay, quantize(ayf, ISMS_ACC_QUANT_G), ISMS_ACC_DEADBAND_G);
        g_s_az = hold_delta(g_s_az, quantize(azf, ISMS_ACC_QUANT_G), ISMS_ACC_DEADBAND_G);
        g_s_gx = hold_delta(g_s_gx, quantize(gxf, ISMS_GYR_QUANT_DPS), ISMS_GYR_DEADBAND_DPS);
        g_s_gy = hold_delta(g_s_gy, quantize(gyf, ISMS_GYR_QUANT_DPS), ISMS_GYR_DEADBAND_DPS);
        g_s_gz = hold_delta(g_s_gz, quantize(gzf, ISMS_GYR_QUANT_DPS), ISMS_GYR_DEADBAND_DPS);
        g_s_angx = hold_delta(g_s_angx, quantize(roll, ISMS_ANG_QUANT_DEG), ISMS_ANG_DEADBAND_DEG);
        g_s_angy = hold_delta(g_s_angy, quantize(pitch, ISMS_ANG_QUANT_DEG), ISMS_ANG_DEADBAND_DEG);
        g_s_angz = hold_delta(g_s_angz, quantize(g_yaw, ISMS_ANG_QUANT_DEG), ISMS_ANG_DEADBAND_DEG);
        *dax = g_s_ax;
        *day = g_s_ay;
        *daz = g_s_az;
        *dgx = g_s_gx;
        *dgy = g_s_gy;
        *dgz = g_s_gz;
        *dangx = g_s_angx;
        *dangy = g_s_angy;
        *dangz = g_s_angz;
    }

    const float dx = axf - g_accel_zero_x;
    const float dy = ayf - g_accel_zero_y;
    const float dz = azf - g_accel_zero_z;
    rms_push(sqrtf(dx * dx + dy * dy + dz * dz));
    *still = g_still;
    return true;
}

#if ISMS_MIC_ENABLE
static bool mic_analyse(float *rms, float *peak_hz)
{
    const int n = ISMS_FFT_N;
    static int16_t *pcm;
    static float *samples;
    if (pcm == NULL) {
        pcm = (int16_t *)heap_caps_malloc((size_t)n * sizeof(int16_t),
                                          MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    }
    if (samples == NULL) {
        samples = (float *)heap_caps_malloc((size_t)n * sizeof(float),
                                            MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    }
    if (pcm == NULL || samples == NULL) {
        return false;
    }
    if (!bsp_es8311_is_ready()) {
        return false;
    }

    int err = bsp_es8311_read_pcm(pcm, (size_t)n * sizeof(int16_t));
    if (err != 0) {
        return false;
    }

    double acc = 0.0;
    for (int i = 0; i < n; i++) {
        float s = (float)pcm[i] / 32768.0f;
        samples[i] = s;
        acc += (double)s * (double)s;
    }
    float r = sqrtf((float)(acc / (double)n));
    if (!isms_finite(r)) {
        r = 0.0f;
    }
    *rms = clampf(r, 0.0f, 1.5f);
    float pk = fft_peak_hz(samples, n, (float)ISMS_AUDIO_FS);
    *peak_hz = clampf(pk, 0.0f, (float)ISMS_AUDIO_FS / 2.0f);
    return true;
}

static void mic_task(void *arg)
{
    (void)arg;
    /* Let QMI finish calibration first. ES8311 I2C shares that bus. */
    int wait = 0;
    while (g_accel_cal_count < ISMS_IMU_CAL_SAMPLES && wait < 200) {
        vTaskDelay(pdMS_TO_TICKS(50));
        wait++;
    }
    vTaskDelay(pdMS_TO_TICKS(250));

    if (g_i2c_bus == NULL || !bsp_i2c_lines_idle()) {
        ESP_LOGE(TAG, "Mic skipped — I2C not idle");
        vTaskDelete(NULL);
        return;
    }
    bsp_es8311_init(g_i2c_bus);
    if (!bsp_es8311_is_ready()) {
        ESP_LOGE(TAG, "ES8311 init failed");
        vTaskDelete(NULL);
        return;
    }
    g_mic_ok = true;
    ESP_LOGI(TAG, "Mic up — quiet for noise floor");

    float floor_sum = 0.0f;
    int floor_n = 0;
    float noise_floor = 0.0f;
    int log_n = 0;
    while (1) {
        float r = 0.0f;
        float pk = 0.0f;
        if (mic_analyse(&r, &pk)) {
            if (!g_mic_calibrated) {
                floor_sum += r;
                floor_n++;
                g_mic_rms = r;
                g_mic_peak = pk;
                if (floor_n >= ISMS_MIC_CAL_SAMPLES) {
                    noise_floor = floor_sum / (float)floor_n;
                    g_mic_calibrated = true;
                    ESP_LOGI(TAG, "Mic calibrated  noise_floor=%.3f", noise_floor);
                }
            } else {
                /* Raw envelope. Do not subtract the floor down to 0.00 — that
                 * made the LCD look frozen after "Mic CAL". */
                g_mic_rms = 0.50f * r + 0.50f * g_mic_rms;
                g_mic_peak = pk;
            }
        }
        if (++log_n >= 12) {
            log_n = 0;
            ESP_LOGI(TAG, "MIC %s  rms=%.3f  peak=%.0f Hz",
                     g_mic_calibrated ? "LIVE" : "CAL",
                     (float)g_mic_rms, (float)g_mic_peak);
        }
        vTaskDelay(pdMS_TO_TICKS(ISMS_MIC_PERIOD_MS));
    }
}
#endif

/* -------------------------------------------------------------------------- */
/* JSON + SPIFFS backup                                                        */
/* -------------------------------------------------------------------------- */

static char *reading_to_json(const isms_reading_t *r)
{
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "timestamp", r->timestamp);
    cJSON_AddStringToObject(root, "zone_id", r->zone_id);

    cJSON *gps = cJSON_AddObjectToObject(root, "gps");
    cJSON_AddNumberToObject(gps, "lat", r->lat);
    cJSON_AddNumberToObject(gps, "lon", r->lon);
    cJSON_AddBoolToObject(gps, "valid", r->gps_valid);

    cJSON *acc = cJSON_AddObjectToObject(root, "accel");
    cJSON_AddNumberToObject(acc, "x", r->accel_x);
    cJSON_AddNumberToObject(acc, "y", r->accel_y);
    cJSON_AddNumberToObject(acc, "z", r->accel_z);

    cJSON *gyr = cJSON_AddObjectToObject(root, "gyro");
    cJSON_AddNumberToObject(gyr, "x", r->gyro_x);
    cJSON_AddNumberToObject(gyr, "y", r->gyro_y);
    cJSON_AddNumberToObject(gyr, "z", r->gyro_z);

    cJSON_AddNumberToObject(root, "acoustic_rms", r->acoustic_rms);
    cJSON_AddNumberToObject(root, "acoustic_peak_freq", r->acoustic_peak_freq);
    cJSON_AddNumberToObject(root, "crack_score", r->crack_score);
    cJSON_AddBoolToObject(root, "crack_detected", r->crack_detected);
    cJSON_AddNumberToObject(root, "crack_count", r->crack_count);
    cJSON_AddNumberToObject(root, "accel_rms_30s", r->accel_rms_30s);
    cJSON_AddBoolToObject(root, "still", r->still);
    cJSON_AddBoolToObject(root, "imu_calibrated", r->imu_calibrated);
    cJSON_AddBoolToObject(root, "mic_ok", r->mic_ok);
    cJSON *ang = cJSON_AddObjectToObject(root, "angle");
    cJSON_AddNumberToObject(ang, "x", r->angle_x);
    cJSON_AddNumberToObject(ang, "y", r->angle_y);
    cJSON_AddNumberToObject(ang, "z", r->angle_z);

    char *txt = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    return txt;
}

static void backup_persist(void)
{
    FILE *f = fopen(ISMS_BACKUP_PATH, "w");
    if (f == NULL) {
        return;
    }
    for (int i = 0; i < g_backup_count; i++) {
        fputs(g_backup[i], f);
        fputc('\n', f);
    }
    fclose(f);
}

static void backup_push(const char *json)
{
    if (g_backup_count < ISMS_BACKUP_MAX) {
        strncpy(g_backup[g_backup_count], json, sizeof(g_backup[0]) - 1);
        g_backup[g_backup_count][sizeof(g_backup[0]) - 1] = '\0';
        g_backup_count++;
    } else {
        memmove(g_backup[0], g_backup[1], (ISMS_BACKUP_MAX - 1) * sizeof(g_backup[0]));
        strncpy(g_backup[ISMS_BACKUP_MAX - 1], json, sizeof(g_backup[0]) - 1);
        g_backup[ISMS_BACKUP_MAX - 1][sizeof(g_backup[0]) - 1] = '\0';
    }
    /* RAM-first: rewriting SPIFFS on every failed POST stalls the HTTP
     * task long enough for the Pi to mark the node offline. */
    static int dirty;
    if (++dirty >= 8) {
        backup_persist();
        dirty = 0;
    }
}

static bool http_post_json(const char *json)
{
    char url[128];
    snprintf(url, sizeof(url), "http://%s:%d%s", ISMS_PI_HOST, ISMS_PI_PORT, ISMS_POST_PATH);

    esp_http_client_config_t cfg = {};
    cfg.url = url;
    cfg.method = HTTP_METHOD_POST;
    cfg.timeout_ms = ISMS_HTTP_TIMEOUT_MS;
    cfg.keep_alive_enable = true;
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        return false;
    }
    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_post_field(client, json, (int)strlen(json));
    esp_err_t err = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    return err == ESP_OK && status >= 200 && status < 300;
}

static void backup_flush(void)
{
    /* At most one replay per live cycle so catch-up cannot block sensor posts. */
    if (g_backup_count <= 0) {
        return;
    }
    if (!http_post_json(g_backup[0])) {
        return;
    }
    memmove(g_backup[0], g_backup[1], (g_backup_count - 1) * sizeof(g_backup[0]));
    g_backup_count--;
    if (g_backup_count == 0) {
        backup_persist();
    }
}

static bool http_get_verdict(isms_verdict_t *out)
{
    char url[128];
    snprintf(url, sizeof(url), "http://%s:%d%s", ISMS_PI_HOST, ISMS_PI_PORT, ISMS_VERDICT_PATH);

    esp_http_client_config_t cfg = {};
    cfg.url = url;
    cfg.method = HTTP_METHOD_GET;
    cfg.timeout_ms = ISMS_HTTP_TIMEOUT_MS;
    cfg.keep_alive_enable = true;
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        return false;
    }
    char body[1024];
    int rd = 0;
    esp_err_t err = esp_http_client_open(client, 0);
    if (err == ESP_OK) {
        (void)esp_http_client_fetch_headers(client);
        rd = esp_http_client_read_response(client, body, sizeof(body) - 1);
    }
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    if (err != ESP_OK || rd <= 0 || status < 200 || status >= 300) {
        return false;
    }
    body[rd] = '\0';

    cJSON *root = cJSON_Parse(body);
    if (root == NULL) {
        return false;
    }
    const cJSON *st = cJSON_GetObjectItem(root, "status");
    const cJSON *ft = cJSON_GetObjectItem(root, "fault_type");
    const cJSON *ttf = cJSON_GetObjectItem(root, "time_to_fault");
    if (cJSON_IsString(st) && st->valuestring) {
        strncpy(out->status, st->valuestring, sizeof(out->status) - 1);
    }
    if (cJSON_IsString(ft) && ft->valuestring) {
        strncpy(out->fault_type, ft->valuestring, sizeof(out->fault_type) - 1);
    } else {
        out->fault_type[0] = '\0';
    }
    if (cJSON_IsString(ttf) && ttf->valuestring) {
        strncpy(out->time_to_fault, ttf->valuestring, sizeof(out->time_to_fault) - 1);
    } else if (cJSON_IsNumber(ttf)) {
        snprintf(out->time_to_fault, sizeof(out->time_to_fault), "%.1f hours", ttf->valuedouble);
    } else {
        strncpy(out->time_to_fault, "None", sizeof(out->time_to_fault) - 1);
    }
    const cJSON *cs = cJSON_GetObjectItem(root, "crack_score");
    const cJSON *cd = cJSON_GetObjectItem(root, "crack_detected");
    if (cJSON_IsNumber(cs)) {
        out->crack_score = (float)cs->valuedouble;
    }
    out->crack_detected = cJSON_IsTrue(cd);
    out->valid = true;
    cJSON_Delete(root);
    return true;
}

/* -------------------------------------------------------------------------- */
/* FreeRTOS tasks                                                              */
/* -------------------------------------------------------------------------- */

static void sensor_task(void *arg)
{
    (void)arg;
    qmi8658_data_t imu;
    double lat = 0.0, lon = 0.0;
    bool gps_ok = false;
    float last_rms = 0.0f;
    float last_peak = 0.0f;
    float last_ax = 0, last_ay = 0, last_az = ISMS_GRAVITY;
    float last_gx = 0, last_gy = 0, last_gz = 0;
    int16_t last_acc_x = 0, last_acc_y = 0, last_acc_z = 0;
    int16_t last_gyr_x = 0, last_gyr_y = 0, last_gyr_z = 0;
    float last_ang_x = 0, last_ang_y = 0, last_ang_z = 0;
    int log_n = 0;

    while (1) {
        /* --- QMI8658 every 50 ms --- */
        TickType_t now_imu = xTaskGetTickCount();
        if (!bsp_qmi8658_is_ready() && g_i2c_bus != NULL) {
            static TickType_t last_qmi_try;
            if ((now_imu - last_qmi_try) >= pdMS_TO_TICKS(3000)) {
                last_qmi_try = now_imu;
                bsp_qmi8658_init(g_i2c_bus);
            }
        }
        bool imu_got = bsp_qmi8658_read_data(&imu);
        bool imu_ok = bsp_qmi8658_is_ready();
        float ax = last_ax, ay = last_ay, az = last_az, gx = last_gx, gy = last_gy, gz = last_gz;
        bool still = g_still;
        int cal_pct = (100 * g_accel_cal_count) / ISMS_IMU_CAL_SAMPLES;
        if (cal_pct > 100) {
            cal_pct = 100;
        }
        if (imu_got) {
            float ax_g = (float)imu.acc_x * ISMS_ACC_SENS_G;
            float ay_g = (float)imu.acc_y * ISMS_ACC_SENS_G;
            float az_g = (float)imu.acc_z * ISMS_ACC_SENS_G;
            float gx_raw = (float)imu.gyr_x * ISMS_GYR_SENS_DPS;
            float gy_raw = (float)imu.gyr_y * ISMS_GYR_SENS_DPS;
            float gz_raw = (float)imu.gyr_z * ISMS_GYR_SENS_DPS;
            if (!isms_finite(ax_g) || !isms_finite(ay_g) || !isms_finite(az_g) ||
                !isms_finite(gx_raw) || !isms_finite(gy_raw) || !isms_finite(gz_raw)) {
                imu_ok = false;
            } else {
                float dax, day, daz, dgx, dgy, dgz, dangx, dangy, dangz;
                imu_calibrate_filter(ax_g, ay_g, az_g, gx_raw, gy_raw, gz_raw,
                                     &dax, &day, &daz, &dgx, &dgy, &dgz,
                                     &dangx, &dangy, &dangz, &still, &cal_pct);
                ax = dax * ISMS_GRAVITY;
                ay = day * ISMS_GRAVITY;
                az = daz * ISMS_GRAVITY;
                gx = dgx;
                gy = dgy;
                gz = dgz;
                last_ax = ax;
                last_ay = ay;
                last_az = az;
                last_gx = gx;
                last_gy = gy;
                last_gz = gz;
                last_acc_x = imu.acc_x;
                last_acc_y = imu.acc_y;
                last_acc_z = imu.acc_z;
                last_gyr_x = imu.gyr_x;
                last_gyr_y = imu.gyr_y;
                last_gyr_z = imu.gyr_z;
                last_ang_x = dangx;
                last_ang_y = dangy;
                last_ang_z = dangz;
            }
        }

        if (++log_n >= 20) {
            log_n = 0;
            ESP_LOGI(TAG,
                     "IMU %s %s  A=%+.2f %+.2f %+.2f g  |a|=%.2f  G=%+.1f %+.1f %+.1f  Mic=%.2f",
                     imu_ok ? "OK" : "STALE",
                     (g_accel_cal_count >= ISMS_IMU_CAL_SAMPLES) ? (still ? "STILL" : "MOVE") : "CAL",
                     last_ax / ISMS_GRAVITY, last_ay / ISMS_GRAVITY, last_az / ISMS_GRAVITY,
                     sqrtf((last_ax * last_ax + last_ay * last_ay + last_az * last_az) /
                           (ISMS_GRAVITY * ISMS_GRAVITY)),
                     last_gx, last_gy, last_gz, last_rms);
        }

        gps_poll(&lat, &lon, &gps_ok);

        last_rms = g_mic_rms;
        last_peak = g_mic_peak;

        char ts[32];
        char zone[16];
        format_timestamp(ts, sizeof(ts));
        assign_zone(lat, lon, gps_ok, zone, sizeof(zone));

        if (xSemaphoreTake(g_data_mux, pdMS_TO_TICKS(20)) == pdTRUE) {
            strncpy(g_reading.timestamp, ts, sizeof(g_reading.timestamp) - 1);
            g_reading.timestamp[sizeof(g_reading.timestamp) - 1] = '\0';
            strncpy(g_reading.zone_id, zone, sizeof(g_reading.zone_id) - 1);
            g_reading.zone_id[sizeof(g_reading.zone_id) - 1] = '\0';
            g_reading.lat = gps_ok ? lat : 0.0;
            g_reading.lon = gps_ok ? lon : 0.0;
            g_reading.gps_valid = gps_ok;
            g_reading.accel_x = ax;
            g_reading.accel_y = ay;
            g_reading.accel_z = az;
            g_reading.gyro_x = gx;
            g_reading.gyro_y = gy;
            g_reading.gyro_z = gz;
            g_reading.acc_raw_x = last_acc_x;
            g_reading.acc_raw_y = last_acc_y;
            g_reading.acc_raw_z = last_acc_z;
            g_reading.gyr_raw_x = last_gyr_x;
            g_reading.gyr_raw_y = last_gyr_y;
            g_reading.gyr_raw_z = last_gyr_z;
            g_reading.angle_x = last_ang_x;
            g_reading.angle_y = last_ang_y;
            g_reading.angle_z = last_ang_z;
            g_reading.acoustic_rms = last_rms;
            g_reading.acoustic_peak_freq = last_peak;
            g_reading.accel_rms_30s = rms_value();
            g_reading.wifi_ok = wifi_has_ip();
            g_reading.imu_ok = imu_ok;
            g_reading.imu_calibrated = (g_accel_cal_count >= ISMS_IMU_CAL_SAMPLES);
            g_reading.still = still;
            g_reading.cal_pct = cal_pct;
            g_reading.mic_ok = g_mic_ok;
            xSemaphoreGive(g_data_mux);
        }

        vTaskDelay(pdMS_TO_TICKS(ISMS_IMU_PERIOD_MS));
    }
}

static void http_post_task(void *arg)
{
    (void)arg;
    while (1) {
        isms_reading_t snap = {};
        if (xSemaphoreTake(g_data_mux, pdMS_TO_TICKS(50)) == pdTRUE) {
            snap = g_reading;
            xSemaphoreGive(g_data_mux);
        }

        if (snap.timestamp[0] == '\0') {
            vTaskDelay(pdMS_TO_TICKS(ISMS_SLOW_PERIOD_MS));
            continue;
        }

        if (!wifi_has_ip() || !snap.imu_calibrated) {
            vTaskDelay(pdMS_TO_TICKS(ISMS_SLOW_PERIOD_MS));
            continue;
        }

        /* Sensor stream owns the radio — wait for camera/verdict to finish. */
        if (xSemaphoreTake(g_http_mux, pdMS_TO_TICKS(3500)) != pdTRUE) {
            ESP_LOGW(TAG, "sensor POST skipped — radio busy");
            vTaskDelay(pdMS_TO_TICKS(ISMS_SLOW_PERIOD_MS));
            continue;
        }
        backup_flush();

        char *json = reading_to_json(&snap);
        if (json) {
            if (!http_post_json(json)) {
                ESP_LOGW(TAG, "POST failed — buffering");
                backup_push(json);
            }
            cJSON_free(json);
        }
        xSemaphoreGive(g_http_mux);
        vTaskDelay(pdMS_TO_TICKS(ISMS_SLOW_PERIOD_MS));
    }
}

static void verdict_task(void *arg)
{
    (void)arg;
    while (1) {
#if ISMS_HTTP_ENABLE
        static int tick = 0;
        if ((tick++ % 40) == 0 && wifi_has_ip()) {
            isms_verdict_t v = {};
            /* Never block the UI loop waiting on HTTP. */
            if (xSemaphoreTake(g_http_mux, pdMS_TO_TICKS(50)) == pdTRUE) {
                bool ok = http_get_verdict(&v);
                xSemaphoreGive(g_http_mux);
                if (ok) {
                    if (xSemaphoreTake(g_data_mux, pdMS_TO_TICKS(50)) == pdTRUE) {
                        g_verdict = v;
                        xSemaphoreGive(g_data_mux);
                    }
                }
            }
        }
#endif
        isms_ui_snapshot_t ui = {};
        if (xSemaphoreTake(g_data_mux, pdMS_TO_TICKS(50)) == pdTRUE) {
            ui.accel_x_g = g_reading.accel_x / ISMS_GRAVITY;
            ui.accel_y_g = g_reading.accel_y / ISMS_GRAVITY;
            ui.accel_z_g = g_reading.accel_z / ISMS_GRAVITY;
            ui.accel_mag_g = sqrtf(ui.accel_x_g * ui.accel_x_g +
                                   ui.accel_y_g * ui.accel_y_g +
                                   ui.accel_z_g * ui.accel_z_g);
            ui.gyro_x_dps = g_reading.gyro_x;
            ui.gyro_y_dps = g_reading.gyro_y;
            ui.gyro_z_dps = g_reading.gyro_z;
            ui.angle_x = g_reading.angle_x;
            ui.angle_y = g_reading.angle_y;
            ui.angle_z = g_reading.angle_z;
            ui.acoustic_rms = g_reading.acoustic_rms;
            ui.acoustic_peak_hz = g_reading.acoustic_peak_freq;
            ui.imu_ok = g_reading.imu_ok;
            ui.imu_calibrated = g_reading.imu_calibrated;
            ui.still = g_reading.still;
            ui.cal_pct = g_reading.cal_pct;
            ui.mic_ok = g_reading.mic_ok;
            ui.mic_calibrated = g_mic_calibrated;
            xSemaphoreGive(g_data_mux);
        }

        if (lvgl_port_lock(50)) {
            isms_ui_update(&ui);
            lvgl_port_unlock();
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

static bool http_post_jpeg(const uint8_t *buf, size_t len, float *score, bool *detected, int *count)
{
    char url[128];
    snprintf(url, sizeof(url), "http://%s:%d%s", ISMS_PI_HOST, ISMS_PI_PORT, ISMS_CAMERA_PATH);
    esp_http_client_config_t cfg = {};
    cfg.url = url;
    cfg.method = HTTP_METHOD_POST;
    cfg.timeout_ms = ISMS_CAM_TIMEOUT_MS;
    cfg.keep_alive_enable = true;
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        return false;
    }
    esp_http_client_set_header(client, "Content-Type", "image/jpeg");
    esp_err_t err = esp_http_client_open(client, (int)len);
    int rd = 0;
    int status = 0;
    char body[512];
    if (err == ESP_OK) {
        (void)esp_http_client_write(client, (const char *)buf, (int)len);
        (void)esp_http_client_fetch_headers(client);
        status = esp_http_client_get_status_code(client);
        rd = esp_http_client_read_response(client, body, sizeof(body) - 1);
    }
    esp_http_client_cleanup(client);
    /* Pi now ACKs immediately (accepted=true) while OpenCV runs in the
       background — treat any 2xx as success even with a short body. */
    if (err != ESP_OK || status < 200 || status >= 300) {
        return false;
    }
    if (rd <= 0) {
        return true;
    }
    body[rd] = '\0';
    cJSON *root = cJSON_Parse(body);
    if (root == NULL) {
        return true;
    }
    const cJSON *cs = cJSON_GetObjectItem(root, "crack_score");
    const cJSON *cd = cJSON_GetObjectItem(root, "crack_detected");
    const cJSON *cc = cJSON_GetObjectItem(root, "crack_count");
    if (cJSON_IsNumber(cs)) {
        *score = (float)cs->valuedouble;
    }
    *detected = cJSON_IsTrue(cd);
    if (cJSON_IsNumber(cc)) {
        *count = cc->valueint;
    }
    cJSON_Delete(root);
    return true;
}

static void camera_task(void *arg)
{
    (void)arg;
    int wait = 0;
    while (g_accel_cal_count < ISMS_IMU_CAL_SAMPLES && wait < 200) {
        vTaskDelay(pdMS_TO_TICKS(50));
        wait++;
    }
    vTaskDelay(pdMS_TO_TICKS(400));
    if (!isms_camera_init()) {
        ESP_LOGE(TAG, "Camera init failed — IMU/mic keep running");
        vTaskDelete(NULL);
        return;
    }
    while (1) {
        if (!wifi_has_ip()) {
            vTaskDelay(pdMS_TO_TICKS(ISMS_CAM_PERIOD_MS));
            continue;
        }
        camera_fb_t *fb = esp_camera_fb_get();
        if (fb == NULL) {
            ESP_LOGW(TAG, "camera frame grab failed");
            vTaskDelay(pdMS_TO_TICKS(ISMS_CAM_PERIOD_MS));
            continue;
        }
        float score = 0.0f;
        bool detected = false;
        int count = 0;
        /* Prefer the 500 ms sensor heartbeat over JPEG — skip this frame
         * if the radio is already in use instead of stacking sockets. */
        bool ok = false;
        if (xSemaphoreTake(g_http_mux, pdMS_TO_TICKS(50)) == pdTRUE) {
            ok = http_post_jpeg(fb->buf, fb->len, &score, &detected, &count);
            xSemaphoreGive(g_http_mux);
        } else {
            ESP_LOGD(TAG, "camera skipped — sensor owns radio");
        }
        esp_camera_fb_return(fb);
        if (ok) {
            if (xSemaphoreTake(g_data_mux, pdMS_TO_TICKS(50)) == pdTRUE) {
                g_reading.crack_score = clampf(score, 0.0f, 1.0f);
                g_reading.crack_detected = detected;
                g_reading.crack_count = count;
                xSemaphoreGive(g_data_mux);
            }
            ESP_LOGI(TAG, "crack score=%.2f detected=%d count=%d", score, (int)detected, count);
        }
        vTaskDelay(pdMS_TO_TICKS(ISMS_CAM_PERIOD_MS));
    }
}

/* -------------------------------------------------------------------------- */
/* Factory display bring-up (unchanged sequence / drivers)                     */
/* -------------------------------------------------------------------------- */

#if ISMS_TOUCH_ENABLE
static void touchpad_read(lv_indev_drv_t *indev_drv, lv_indev_data_t *data)
{
    (void)indev_drv;
    static lv_coord_t last_x = 0;
    static lv_coord_t last_y = 0;
    touch_data_t touch_data;
    bsp_touch_read();
    if (bsp_touch_get_coordinates(&touch_data)) {
        last_x = touch_data.coords[0].x;
        last_y = touch_data.coords[0].y;
        data->state = LV_INDEV_STATE_PR;
    } else {
        data->state = LV_INDEV_STATE_REL;
    }
    data->point.x = last_x;
    data->point.y = last_y;
}
#endif

static void lv_port_bringup(void)
{
    lvgl_port_cfg_t port_cfg = {};
    port_cfg.task_priority = 4;
    port_cfg.task_stack = 1024 * 5;
    port_cfg.task_affinity = 1;
    port_cfg.task_max_sleep_ms = 500;
    port_cfg.timer_period_ms = 5;
    lvgl_port_init(&port_cfg);

    lvgl_port_display_cfg_t disp_cfg = {};
    disp_cfg.io_handle = io_handle;
    disp_cfg.panel_handle = panel_handle;
    disp_cfg.buffer_size = ISMS_LCD_BUFFER_SIZE;
    disp_cfg.sw_rotate = LV_DISP_ROT_NONE;
    disp_cfg.hres = ISMS_LCD_H_RES;
    disp_cfg.vres = ISMS_LCD_V_RES;
    disp_cfg.trans_size = ISMS_LCD_BUFFER_SIZE / 10;
    disp_cfg.draw_wait_cb = NULL;
    disp_cfg.flags.buff_dma = false;
    disp_cfg.flags.buff_spiram = true;
    lvgl_disp = lvgl_port_add_disp(&disp_cfg);

#if ISMS_TOUCH_ENABLE
    static lv_indev_drv_t indev_drv;
    lv_indev_drv_init(&indev_drv);
    indev_drv.type = LV_INDEV_TYPE_POINTER;
    indev_drv.read_cb = touchpad_read;
    lvgl_touch_indev = lv_indev_drv_register(&indev_drv);
    if (lvgl_touch_indev != NULL && lvgl_touch_indev->driver != NULL &&
        lvgl_touch_indev->driver->read_timer != NULL) {
        lv_timer_set_period(lvgl_touch_indev->driver->read_timer, 50);
    }
#endif
}

static bool io_expander_init(i2c_master_bus_handle_t bus_handle)
{
    /* LCD reset is on TCA9554 P1 — same pulse as factory firmware */
    esp_err_t err = esp_io_expander_new_i2c_tca9554(bus_handle, ESP_IO_EXPANDER_I2C_TCA9554_ADDRESS_000, &expander_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "TCA9554 init failed (%s) — not aborting", esp_err_to_name(err));
        expander_handle = NULL;
        return false;
    }
    ESP_ERROR_CHECK(esp_io_expander_set_dir(expander_handle, IO_EXPANDER_PIN_NUM_1, IO_EXPANDER_OUTPUT));
    ESP_ERROR_CHECK(esp_io_expander_set_level(expander_handle, IO_EXPANDER_PIN_NUM_1, 0));
    vTaskDelay(pdMS_TO_TICKS(100));
    ESP_ERROR_CHECK(esp_io_expander_set_level(expander_handle, IO_EXPANDER_PIN_NUM_1, 1));
    vTaskDelay(pdMS_TO_TICKS(200));
    return true;
}

static void peripherals_init(void)
{
#if ISMS_GPS_ENABLE
    uart_config_t uc = {};
    uc.baud_rate = ISMS_GPS_BAUD;
    uc.data_bits = UART_DATA_8_BITS;
    uc.parity = UART_PARITY_DISABLE;
    uc.stop_bits = UART_STOP_BITS_1;
    uc.flow_ctrl = UART_HW_FLOWCTRL_DISABLE;
    uc.source_clk = UART_SCLK_DEFAULT;
    ESP_ERROR_CHECK(uart_driver_install(ISMS_GPS_UART, 2048, 0, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(ISMS_GPS_UART, &uc));
    ESP_ERROR_CHECK(uart_set_pin(ISMS_GPS_UART, ISMS_GPS_TX_GPIO, ISMS_GPS_RX_GPIO,
                                 UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));
#else
    ESP_LOGI(TAG, "GPS disabled (GPIO44 reserved for ES8311 MCLK)");
#endif
}

static void spiffs_init(void)
{
    esp_vfs_spiffs_conf_t conf = {};
    conf.base_path = "/spiffs";
    conf.partition_label = "storage";
    conf.max_files = 4;
    conf.format_if_mount_failed = true;
    esp_err_t ret = esp_vfs_spiffs_register(&conf);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "SPIFFS mount failed (%s) — RAM backup only", esp_err_to_name(ret));
        return;
    }
    FILE *f = fopen(ISMS_BACKUP_PATH, "r");
    if (f) {
        char line[768];
        while (fgets(line, sizeof(line), f) && g_backup_count < ISMS_BACKUP_MAX) {
            size_t n = strlen(line);
            while (n && (line[n - 1] == '\n' || line[n - 1] == '\r')) {
                line[--n] = '\0';
            }
            if (n > 2) {
                strncpy(g_backup[g_backup_count], line, sizeof(g_backup[0]) - 1);
                g_backup_count++;
            }
        }
        fclose(f);
        ESP_LOGI(TAG, "Restored %d buffered readings", g_backup_count);
    }
}

static void isms_time_sync_init(void)
{
    esp_sntp_setoperatingmode(SNTP_OPMODE_POLL);
    esp_sntp_setservername(0, "pool.ntp.org");
    esp_sntp_init();
    setenv("TZ", "UTC", 1);
    tzset();
}

extern "C" void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    memset(&g_reading, 0, sizeof(g_reading));
    strncpy(g_reading.zone_id, "ZONE_UNKNOWN", sizeof(g_reading.zone_id) - 1);
    strncpy(g_verdict.status, "NORMAL", sizeof(g_verdict.status) - 1);
    g_data_mux = xSemaphoreCreateMutex();
    g_http_mux = xSemaphoreCreateMutex();
    assert(g_data_mux && g_http_mux);

    /* Keep the console on IMU/ISMS only — I2C NACKs from other chips are noise. */
    esp_log_level_set("i2c.master", ESP_LOG_NONE);
    esp_log_level_set("tca9554", ESP_LOG_ERROR);

    /* I2C (SDA=GPIO8, SCL=GPIO7). IMU + PMU + LCD expander only. */
    g_i2c_bus = bsp_i2c_init();
    i2c_master_bus_handle_t i2c_bus_handle = g_i2c_bus;
    vTaskDelay(pdMS_TO_TICKS(20));

    const bool i2c_idle = bsp_i2c_lines_idle();
    if (!i2c_idle) {
        ESP_LOGE(TAG, "I2C SDA/SCL still low — skip AXP/QMI. Press PWR, then RESET");
    } else {
        if (bsp_axp2101_init(i2c_bus_handle) != ESP_OK) {
            ESP_LOGW(TAG, "AXP2101 failed — IMU rails may already be on, continuing");
        }
        vTaskDelay(pdMS_TO_TICKS(120));
        bsp_qmi8658_init(i2c_bus_handle);
        io_expander_init(i2c_bus_handle);
    }

    bsp_display_init(&io_handle, &panel_handle, ISMS_LCD_BUFFER_SIZE);
#if ISMS_TOUCH_ENABLE
    bsp_touch_init(i2c_bus_handle, ISMS_LCD_H_RES, ISMS_LCD_V_RES, 0);
#endif

    bsp_wifi_init(ISMS_WIFI_SSID, ISMS_WIFI_PASSWORD);
    isms_time_sync_init();

#if ISMS_MIC_ENABLE
    ESP_LOGI(TAG, "Mic task starts after IMU calibration");
#else
    ESP_LOGI(TAG, "Mic disabled — I2C left for IMU");
#endif

    bsp_display_brightness_init();
    bsp_display_set_brightness(100);

    lv_port_bringup();
    peripherals_init();
    spiffs_init();

    if (lvgl_port_lock(0)) {
        isms_ui_init();
        lvgl_port_unlock();
    }

    /* Live LCD + HTTP POST to the CIC at ISMS_PI_HOST:ISMS_PI_PORT. */
    xTaskCreatePinnedToCore(sensor_task, "isms_sens", 8192, NULL, 3, NULL, 0);
    xTaskCreatePinnedToCore(verdict_task, "isms_ui", 8192, NULL, 1, NULL, 1);
#if ISMS_MIC_ENABLE
    xTaskCreatePinnedToCore(mic_task, "isms_mic", 8192, NULL, 2, NULL, 1);
#endif
#if ISMS_HTTP_ENABLE
    xTaskCreatePinnedToCore(http_post_task, "isms_post", 8192, NULL, 2, NULL, 1);
#endif
#if ISMS_CAMERA_ENABLE
    /* Priority below sensor POST so live telemetry wins scheduling. */
    xTaskCreatePinnedToCore(camera_task, "isms_cam", 8192, NULL, 1, NULL, 1);
    bool cam_ok = true;
#else
    bool cam_ok = false;
#endif

    ESP_LOGI(TAG, "ISMS LIVE display  HTTP=%d camera=%d  host=%s:%d",
             ISMS_HTTP_ENABLE, (int)cam_ok, ISMS_PI_HOST, ISMS_PI_PORT);
}

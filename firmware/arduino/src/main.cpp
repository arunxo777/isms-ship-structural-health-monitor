/*
 * ISMS Arduino-framework node (ESP32-S3).
 *
 * Prefer flashing ESP-IDF/01_factory on the Waveshare 3.5B — that project
 * reuses the official AXS15231B / QMI8658 / ES8311 / AXP2101 BSP and
 * does not retouch the QSPI display driver.
 *
 * This sketch is the same application logic for PlatformIO/Arduino:
 * sensors, FFT, GPS zones, HTTP POST/GET, SPIFFS backup, FreeRTOS tasks.
 * Display widgets are created only if LVGL is already in the build
 * (do NOT call lcd init from here).
 */

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <TinyGPSPlus.h>
#include <SPIFFS.h>
#include <Wire.h>
#include <math.h>
#include <arduinoFFT.h>
#include "driver/i2s.h"
#include "esp_heap_caps.h"

#if __has_include(<lvgl.h>)
#include <lvgl.h>
#define ISMS_HAS_LVGL 1
#else
#define ISMS_HAS_LVGL 0
#endif

/* ---- Network: copy secrets.example.h to secrets.h (git-ignored) ---- */
#if __has_include("secrets.h")
#include "secrets.h"
#endif
#ifndef ISMS_WIFI_SSID
#define ISMS_WIFI_SSID "your-wifi-ssid"
#endif
#ifndef ISMS_WIFI_PASSWORD
#define ISMS_WIFI_PASSWORD "your-wifi-password"
#endif
#ifndef ISMS_PI_HOST
#define ISMS_PI_HOST "192.168.1.100"
#endif
static const char *WIFI_SSID = ISMS_WIFI_SSID;
static const char *WIFI_PASS = ISMS_WIFI_PASSWORD;
static const char *PI_HOST = ISMS_PI_HOST;
static const uint16_t PI_PORT = 8000;

/* ---- Onboard I2C (factory: SDA=8 SCL=7) ---- */
static const int PIN_SDA = 8;
static const int PIN_SCL = 7;
static const uint8_t QMI8658_ADDR = 0x6B;
static const uint8_t ES8311_ADDR = 0x18;
static const uint8_t AXP2101_ADDR = 0x34;

/* Factory ES8311 I2S — do not use the generic 15/16/17 mapping, it is wrong
 * on this board. BCLK=13, LRCK=15, DOUT=16 (mic->SoC), DIN=14, MCLK=44. */
static const int I2S_MCLK = 44;
static const int I2S_BCLK = 13;
static const int I2S_LRCK = 15;
static const int I2S_DOUT = 16;
static const int I2S_DIN = 14;
static const int AUDIO_FS = 16000;
static const int FFT_N = 512;

/* External sensors — HC-SR04 dropped so GPIO40/41 stay on the camera DVP bus.
 * Camera capture lives in the ESP-IDF factory firmware (isms_camera.c). */
static const int GPS_RX = 44;
static const int GPS_TX = 43;
static const uint32_t GPS_BAUD = 9600;

static const int RMS_WINDOW = 600;
static const int BACKUP_MAX = 10;

static SemaphoreHandle_t g_mux;

struct Reading {
  char timestamp[32];
  char zone_id[16];
  double lat;
  double lon;
  bool gps_valid;
  float accel_x, accel_y, accel_z;
  float gyro_x, gyro_y, gyro_z;
  float acoustic_rms;
  float acoustic_peak_freq;
  float crack_score;
  bool crack_detected;
  float accel_rms_30s;
  bool wifi_ok;
};

struct Verdict {
  char status[16];
  char fault_type[96];
  char time_to_fault[48];
  bool valid;
};

static Reading g_reading;
static Verdict g_verdict;
static TinyGPSPlus gps;
static HardwareSerial GPSSerial(2);

static float g_rms_buf[RMS_WINDOW];
static int g_rms_head = 0, g_rms_count = 0;
static double g_rms_sum_sq = 0;

static String g_backup[BACKUP_MAX];
static int g_backup_n = 0;

static float vReal[FFT_N];
static float vImag[FFT_N];
static ArduinoFFT<float> FFT = ArduinoFFT<float>(vReal, vImag, FFT_N, AUDIO_FS);

#if ISMS_HAS_LVGL
static lv_obj_t *ui_status;
static lv_obj_t *ui_fault;
static lv_obj_t *ui_pred;
static lv_obj_t *ui_bottom;
static lv_obj_t *ui_wifi;
static lv_obj_t *ui_bars[3];
#endif

/* -------------------------------------------------------------------------- */
static bool qmi_write(uint8_t reg, uint8_t val)
{
  Wire.beginTransmission(QMI8658_ADDR);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

static bool qmi_read(uint8_t reg, uint8_t *buf, size_t n)
{
  Wire.beginTransmission(QMI8658_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) {
    return false;
  }
  if (Wire.requestFrom((int)QMI8658_ADDR, (int)n) != (int)n) {
    return false;
  }
  for (size_t i = 0; i < n; i++) {
    buf[i] = Wire.read();
  }
  return true;
}

static bool qmi_init()
{
  uint8_t id = 0;
  if (!qmi_read(0x00, &id, 1) || id != 0x05) {
    Serial.printf("QMI8658 not found (id=0x%02X)\n", id);
    return false;
  }
  qmi_write(0x60, 0xB0); /* reset */
  delay(10);
  qmi_write(0x02, 0x40); /* CTRL1 auto-increment */
  qmi_write(0x08, 0x03); /* CTRL7 acc+gyro on */
  qmi_write(0x03, 0x95); /* CTRL2 ±4 g, 250 Hz */
  qmi_write(0x04, 0xD5); /* CTRL3 ±512 dps, 250 Hz */
  Serial.println("QMI8658 ready ±4g ±512dps");
  return true;
}

static bool qmi_read_ms2_dps(float *ax, float *ay, float *az, float *gx, float *gy, float *gz)
{
  uint8_t st = 0;
  if (!qmi_read(0x2E, &st, 1) || (st & 0x03) == 0) {
    return false;
  }
  uint8_t raw[12];
  if (!qmi_read(0x35, raw, 12)) {
    return false;
  }
  auto s16 = [](uint8_t lo, uint8_t hi) -> int16_t {
    return (int16_t)((uint16_t)lo | ((uint16_t)hi << 8));
  };
  const float acc_s = 4.0f / 32768.0f;
  const float gyr_s = 512.0f / 32768.0f;
  const float g = 9.80665f;
  *ax = s16(raw[0], raw[1]) * acc_s * g;
  *ay = s16(raw[2], raw[3]) * acc_s * g;
  *az = s16(raw[4], raw[5]) * acc_s * g;
  *gx = s16(raw[6], raw[7]) * gyr_s;
  *gy = s16(raw[8], raw[9]) * gyr_s;
  *gz = s16(raw[10], raw[11]) * gyr_s;
  return isfinite(*ax) && isfinite(*ay) && isfinite(*az);
}

/* Minimal ES8311 bring-up on the factory I2C address; I2S clocks come from the SoC. */
static void es8311_write(uint8_t reg, uint8_t val)
{
  Wire.beginTransmission(ES8311_ADDR);
  Wire.write(reg);
  Wire.write(val);
  Wire.endTransmission();
}

static void es8311_init()
{
  es8311_write(0x00, 0x1F); /* reset */
  delay(10);
  es8311_write(0x00, 0x80);
  es8311_write(0x01, 0x3F);
  es8311_write(0x16, 0x24);
  es8311_write(0x04, 0x18);
  es8311_write(0x05, 0x00);
  es8311_write(0x0C, 0x00);
  es8311_write(0x10, 0x1F);
  es8311_write(0x11, 0x7F);
  es8311_write(0x12, 0x00);
  es8311_write(0x13, 0x00);
  es8311_write(0x09, 0x01); /* ADC on */
  es8311_write(0x0A, 0x01);
  es8311_write(0x17, 0xBF); /* ADC volume */
}

static bool i2s_init()
{
  i2s_config_t cfg = {};
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
  cfg.sample_rate = AUDIO_FS;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_ONLY_LEFT;
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count = 4;
  cfg.dma_buf_len = FFT_N;
  cfg.use_apll = false;
  cfg.tx_desc_auto_clear = false;
  cfg.fixed_mclk = AUDIO_FS * 256;

  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_MCLK;
  pins.bck_io_num = I2S_BCLK;
  pins.ws_io_num = I2S_LRCK;
  pins.data_out_num = I2S_PIN_NO_CHANGE;
  pins.data_in_num = I2S_DOUT; /* factory: codec drives DOUT=GPIO16 into SoC DIN */

  if (i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL) != ESP_OK) {
    return false;
  }
  return i2s_set_pin(I2S_NUM_0, &pins) == ESP_OK;
}

static bool finite_f(float v) { return isfinite(v); }

static void rms_push(float mag)
{
  if (!finite_f(mag)) {
    return;
  }
  if (g_rms_count == RMS_WINDOW) {
    float old = g_rms_buf[g_rms_head];
    g_rms_sum_sq -= (double)old * (double)old;
  } else {
    g_rms_count++;
  }
  g_rms_buf[g_rms_head] = mag;
  g_rms_sum_sq += (double)mag * (double)mag;
  g_rms_head = (g_rms_head + 1) % RMS_WINDOW;
}

static float rms_value()
{
  if (g_rms_count <= 0) {
    return 0.0f;
  }
  return sqrtf((float)(g_rms_sum_sq / (double)g_rms_count));
}

static void format_ts(char *buf, size_t n)
{
  time_t now = time(nullptr);
  struct tm tmv;
  gmtime_r(&now, &tmv);
  if (tmv.tm_year + 1900 < 2025) {
    unsigned long sec = millis() / 1000;
    snprintf(buf, n, "2026-09-09T%02lu:%02lu:%02lu",
             (sec / 3600) % 24, (sec / 60) % 60, sec % 60);
    return;
  }
  strftime(buf, n, "%Y-%m-%dT%H:%M:%S", &tmv);
}

static void assign_zone(double lat, double lon, bool ok, char *zone, size_t n)
{
  if (!ok) {
    strncpy(zone, "ZONE_UNKNOWN", n - 1);
  } else if (lat >= 9.90 && lat <= 9.95 && lon >= 78.10 && lon <= 78.15) {
    strncpy(zone, "ZONE_A", n - 1);
  } else if (lat >= 9.95 && lat <= 10.00 && lon >= 78.15 && lon <= 78.20) {
    strncpy(zone, "ZONE_B", n - 1);
  } else {
    strncpy(zone, "ZONE_C", n - 1);
  }
  zone[n - 1] = '\0';
}

/* Ultrasonic removed — camera DVP owns GPIO40/41 in the IDF firmware. */

static void mic_analyse(float *rms, float *peak)
{
  int16_t pcm[FFT_N];
  size_t got = 0;
  if (i2s_read(I2S_NUM_0, pcm, sizeof(pcm), &got, pdMS_TO_TICKS(80)) != ESP_OK || got < sizeof(pcm)) {
    return;
  }
  double acc = 0;
  for (int i = 0; i < FFT_N; i++) {
    float s = pcm[i] / 32768.0f;
    vReal[i] = s;
    vImag[i] = 0;
    acc += (double)s * (double)s;
  }
  *rms = constrain(sqrtf((float)(acc / FFT_N)), 0.0f, 1.5f);
  FFT.windowing(FFTWindow::Hamming, FFTDirection::Forward);
  FFT.compute(FFTDirection::Forward);
  FFT.complexToMagnitude();
  *peak = FFT.majorPeak();
  if (!finite_f(*peak)) {
    *peak = 0;
  }
}

static String reading_json(const Reading &r)
{
  StaticJsonDocument<768> doc;
  doc["timestamp"] = r.timestamp;
  doc["zone_id"] = r.zone_id;
  JsonObject gpsj = doc.createNestedObject("gps");
  gpsj["lat"] = r.lat;
  gpsj["lon"] = r.lon;
  gpsj["valid"] = r.gps_valid;
  JsonObject a = doc.createNestedObject("accel");
  a["x"] = r.accel_x;
  a["y"] = r.accel_y;
  a["z"] = r.accel_z;
  JsonObject g = doc.createNestedObject("gyro");
  g["x"] = r.gyro_x;
  g["y"] = r.gyro_y;
  g["z"] = r.gyro_z;
  doc["acoustic_rms"] = r.acoustic_rms;
  doc["acoustic_peak_freq"] = r.acoustic_peak_freq;
  doc["crack_score"] = r.crack_score;
  doc["crack_detected"] = r.crack_detected;
  doc["accel_rms_30s"] = r.accel_rms_30s;
  String out;
  serializeJson(doc, out);
  return out;
}

static void backup_persist()
{
  File f = SPIFFS.open("/backup.jsonl", FILE_WRITE);
  if (!f) {
    return;
  }
  for (int i = 0; i < g_backup_n; i++) {
    f.println(g_backup[i]);
  }
  f.close();
}

static void backup_push(const String &json)
{
  if (g_backup_n < BACKUP_MAX) {
    g_backup[g_backup_n++] = json;
  } else {
    for (int i = 1; i < BACKUP_MAX; i++) {
      g_backup[i - 1] = g_backup[i];
    }
    g_backup[BACKUP_MAX - 1] = json;
  }
  backup_persist();
}

static bool http_post(const String &json)
{
  HTTPClient http;
  String url = String("http://") + PI_HOST + ":" + String(PI_PORT) + "/sensor-data";
  http.setTimeout(2000);
  if (!http.begin(url)) {
    return false;
  }
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(json);
  http.end();
  return code >= 200 && code < 300;
}

static void backup_flush()
{
  while (g_backup_n > 0) {
    if (!http_post(g_backup[0])) {
      return;
    }
    for (int i = 1; i < g_backup_n; i++) {
      g_backup[i - 1] = g_backup[i];
    }
    g_backup_n--;
  }
  backup_persist();
}

static bool http_get_verdict(Verdict *out)
{
  HTTPClient http;
  String url = String("http://") + PI_HOST + ":" + String(PI_PORT) + "/latest-verdict";
  http.setTimeout(2000);
  if (!http.begin(url)) {
    return false;
  }
  int code = http.GET();
  if (code < 200 || code >= 300) {
    http.end();
    return false;
  }
  String body = http.getString();
  http.end();
  StaticJsonDocument<1024> doc;
  if (deserializeJson(doc, body)) {
    return false;
  }
  strlcpy(out->status, doc["status"] | "NORMAL", sizeof(out->status));
  strlcpy(out->fault_type, doc["fault_type"] | "", sizeof(out->fault_type));
  if (doc["time_to_fault"].is<const char *>()) {
    strlcpy(out->time_to_fault, doc["time_to_fault"].as<const char *>(), sizeof(out->time_to_fault));
  } else {
    strlcpy(out->time_to_fault, "None", sizeof(out->time_to_fault));
  }
  out->valid = true;
  return true;
}

#if ISMS_HAS_LVGL
/* Widgets only — caller must already have initialised LVGL + AXS15231B. */
static void ui_init()
{
  lv_obj_t *scr = lv_scr_act();
  lv_obj_set_style_bg_color(scr, lv_color_hex(0x0A1628), 0);
  lv_obj_t *title = lv_label_create(scr);
  lv_label_set_text(title, "ISMS NODE");
  lv_obj_set_style_text_color(title, lv_color_hex(0xFFFFFF), 0);
  lv_obj_align(title, LV_ALIGN_TOP_LEFT, 8, 8);
  ui_wifi = lv_label_create(scr);
  lv_obj_align(ui_wifi, LV_ALIGN_TOP_RIGHT, -8, 8);
  ui_status = lv_label_create(scr);
  lv_obj_set_style_text_font(ui_status, &lv_font_montserrat_20, 0);
  lv_obj_align(ui_status, LV_ALIGN_CENTER, 0, -20);
  ui_fault = lv_label_create(scr);
  lv_obj_set_width(ui_fault, 280);
  lv_label_set_long_mode(ui_fault, LV_LABEL_LONG_WRAP);
  lv_obj_align(ui_fault, LV_ALIGN_CENTER, 0, 20);
  ui_pred = lv_label_create(scr);
  lv_obj_align(ui_pred, LV_ALIGN_CENTER, 0, 70);
  ui_bottom = lv_label_create(scr);
  lv_obj_align(ui_bottom, LV_ALIGN_BOTTOM_MID, 0, -8);
  for (int i = 0; i < 3; i++) {
    ui_bars[i] = lv_bar_create(scr);
    lv_obj_set_size(ui_bars[i], 18, 70);
    lv_obj_align(ui_bars[i], LV_ALIGN_TOP_MID, -90 + i * 90, 40);
    lv_bar_set_range(ui_bars[i], 0, 100);
  }
}

static void ui_update(const Reading &r, const Verdict &v)
{
  lv_label_set_text(ui_wifi, r.wifi_ok ? "WiFi ON" : "WiFi --");
  lv_bar_set_value(ui_bars[0], (int)constrain(r.accel_rms_30s / 2.0f * 100.0f, 0, 100), LV_ANIM_ON);
  lv_bar_set_value(ui_bars[1], (int)constrain(r.acoustic_rms * 100.0f, 0, 100), LV_ANIM_ON);
  lv_bar_set_value(ui_bars[2], (int)constrain(r.crack_score * 100.0f, 0, 100), LV_ANIM_ON);
  const char *st = v.valid ? v.status : "NORMAL";
  if (strcmp(st, "CRITICAL") == 0) {
    lv_label_set_text(ui_status, "CRITICAL");
    lv_label_set_text(ui_fault, v.fault_type);
  } else if (strcmp(st, "WARNING") == 0) {
    lv_label_set_text(ui_status, "WARNING");
    lv_label_set_text(ui_fault, v.fault_type);
  } else {
    lv_label_set_text(ui_status, "NORMAL");
    lv_label_set_text(ui_fault, "Hull signature stable");
  }
  if (v.valid && strlen(v.time_to_fault) && strcmp(v.time_to_fault, "None") != 0) {
    char b[80];
    snprintf(b, sizeof(b), "Est. time to fault: %s", v.time_to_fault);
    lv_label_set_text(ui_pred, b);
  } else {
    lv_label_set_text(ui_pred, "No fault predicted");
  }
  char bot[96];
  if (r.gps_valid) {
    snprintf(bot, sizeof(bot), "%s  %.4f, %.4f  %s", r.zone_id, r.lat, r.lon, r.timestamp);
  } else {
    snprintf(bot, sizeof(bot), "%s  NO FIX  %s", r.zone_id, r.timestamp);
  }
  lv_label_set_text(ui_bottom, bot);
}
#endif

static void sensor_task(void *arg)
{
  (void)arg;
  float last_rms = 0, last_pk = 0;
  TickType_t last_slow = 0;
  const TickType_t slow = pdMS_TO_TICKS(500);
  while (true) {
    float ax = 0, ay = 0, az = 9.81f, gx = 0, gy = 0, gz = 0;
    if (qmi_read_ms2_dps(&ax, &ay, &az, &gx, &gy, &gz)) {
      float mag = sqrtf((ax * ax + ay * ay + az * az)) / 9.80665f;
      rms_push(mag);
    }
    while (GPSSerial.available()) {
      gps.encode((char)GPSSerial.read());
    }
    TickType_t now = xTaskGetTickCount();
    if ((now - last_slow) >= slow) {
      last_slow = now;
      mic_analyse(&last_rms, &last_pk);
    }

    char ts[32], zone[16];
    format_ts(ts, sizeof(ts));
    bool fix = gps.location.isValid();
    assign_zone(gps.location.lat(), gps.location.lng(), fix, zone, sizeof(zone));

    if (xSemaphoreTake(g_mux, pdMS_TO_TICKS(20)) == pdTRUE) {
      strlcpy(g_reading.timestamp, ts, sizeof(g_reading.timestamp));
      strlcpy(g_reading.zone_id, zone, sizeof(g_reading.zone_id));
      g_reading.lat = fix ? gps.location.lat() : 0;
      g_reading.lon = fix ? gps.location.lng() : 0;
      g_reading.gps_valid = fix;
      g_reading.accel_x = ax;
      g_reading.accel_y = ay;
      g_reading.accel_z = az;
      g_reading.gyro_x = gx;
      g_reading.gyro_y = gy;
      g_reading.gyro_z = gz;
      g_reading.acoustic_rms = last_rms;
      g_reading.acoustic_peak_freq = last_pk;
      g_reading.accel_rms_30s = rms_value();
      g_reading.wifi_ok = WiFi.status() == WL_CONNECTED;
      xSemaphoreGive(g_mux);
    }
    vTaskDelay(pdMS_TO_TICKS(50));
  }
}

static void http_post_task(void *arg)
{
  (void)arg;
  while (true) {
    Reading snap = {};
    if (xSemaphoreTake(g_mux, pdMS_TO_TICKS(50)) == pdTRUE) {
      snap = g_reading;
      xSemaphoreGive(g_mux);
    }
    if (snap.timestamp[0] == 0) {
      vTaskDelay(pdMS_TO_TICKS(500));
      continue;
    }
    String json = reading_json(snap);
    if (WiFi.status() != WL_CONNECTED) {
      WiFi.begin(WIFI_SSID, WIFI_PASS);
      backup_push(json);
    } else {
      backup_flush();
      if (!http_post(json)) {
        backup_push(json);
      }
    }
    vTaskDelay(pdMS_TO_TICKS(500));
  }
}

static void verdict_task(void *arg)
{
  (void)arg;
  int tick = 0;
  while (true) {
    if ((tick % 2) == 0 && WiFi.status() == WL_CONNECTED) {
      Verdict v = {};
      if (http_get_verdict(&v)) {
        if (xSemaphoreTake(g_mux, pdMS_TO_TICKS(50)) == pdTRUE) {
          g_verdict = v;
          xSemaphoreGive(g_mux);
        }
      }
    }
    Reading r = {};
    Verdict v = {};
    if (xSemaphoreTake(g_mux, pdMS_TO_TICKS(50)) == pdTRUE) {
      r = g_reading;
      v = g_verdict;
      xSemaphoreGive(g_mux);
    }
#if ISMS_HAS_LVGL
    ui_update(r, v);
#else
    Serial.printf("%s %s rms=%.3f ac=%.3f crack=%.2f %s\n",
                  r.timestamp, r.zone_id, r.accel_rms_30s, r.acoustic_rms,
                  r.crack_score, v.valid ? v.status : "n/a");
#endif
    tick++;
    vTaskDelay(pdMS_TO_TICKS(1000));
  }
}

void setup()
{
  Serial.begin(115200);
  delay(200);
  Serial.println("ISMS Arduino node boot");

  g_mux = xSemaphoreCreateMutex();
  memset(&g_reading, 0, sizeof(g_reading));
  strlcpy(g_reading.zone_id, "ZONE_UNKNOWN", sizeof(g_reading.zone_id));
  strlcpy(g_verdict.status, "NORMAL", sizeof(g_verdict.status));

  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(400000);
  Wire.beginTransmission(AXP2101_ADDR);
  if (Wire.endTransmission() == 0) {
    Serial.println("AXP2101 present on 0x34");
  }

  qmi_init();
  es8311_init();
  i2s_init();

  GPSSerial.begin(GPS_BAUD, SERIAL_8N1, GPS_RX, GPS_TX);

  if (!SPIFFS.begin(true)) {
    Serial.println("SPIFFS mount failed");
  } else {
    File f = SPIFFS.open("/backup.jsonl", FILE_READ);
    if (f) {
      while (f.available() && g_backup_n < BACKUP_MAX) {
        String line = f.readStringUntil('\n');
        line.trim();
        if (line.length() > 2) {
          g_backup[g_backup_n++] = line;
        }
      }
      f.close();
    }
  }

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

#if ISMS_HAS_LVGL
  /* Display driver must already be running — we only attach widgets. */
  ui_init();
#endif

  xTaskCreatePinnedToCore(sensor_task, "isms_sens", 8192, NULL, 3, NULL, 0);
  xTaskCreatePinnedToCore(http_post_task, "isms_post", 8192, NULL, 2, NULL, 1);
  xTaskCreatePinnedToCore(verdict_task, "isms_ui", 8192, NULL, 1, NULL, 1);
}

void loop()
{
  /* All work is in FreeRTOS tasks. */
  vTaskDelay(pdMS_TO_TICKS(1000));
}

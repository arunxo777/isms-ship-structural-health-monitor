/*
 * Camera init for ISMS — JPEG QVGA for crack-detection upload.
 * Uses the same DVP pins as the Waveshare factory BSP.
 * Does NOT touch the AXS15231B display driver.
 *
 * GPIO40/41 are camera Y7/PCLK (previously used for HC-SR04).
 */

#include "isms_camera.h"

#include "esp_camera.h"
#include "esp_log.h"
#include "driver/ledc.h"

static const char *TAG = "isms_cam";

#define PWDN_GPIO_NUM    -1
#define RESET_GPIO_NUM   -1
#define XCLK_GPIO_NUM    38
#define Y9_GPIO_NUM      21
#define Y8_GPIO_NUM      39
#define Y7_GPIO_NUM      40
#define Y6_GPIO_NUM      42
#define Y5_GPIO_NUM      46
#define Y4_GPIO_NUM      48
#define Y3_GPIO_NUM      47
#define Y2_GPIO_NUM      45
#define VSYNC_GPIO_NUM   17
#define HREF_GPIO_NUM    18
#define PCLK_GPIO_NUM    41

bool isms_camera_init(void)
{
    camera_config_t config = {};
    /* CHANNEL_2 avoids clashing with LCD backlight on LEDC channel 0 */
    config.ledc_channel = LEDC_CHANNEL_2;
    config.ledc_timer = LEDC_TIMER_1;
    config.pin_d0 = Y2_GPIO_NUM;
    config.pin_d1 = Y3_GPIO_NUM;
    config.pin_d2 = Y4_GPIO_NUM;
    config.pin_d3 = Y5_GPIO_NUM;
    config.pin_d4 = Y6_GPIO_NUM;
    config.pin_d5 = Y7_GPIO_NUM;
    config.pin_d6 = Y8_GPIO_NUM;
    config.pin_d7 = Y9_GPIO_NUM;
    config.pin_xclk = XCLK_GPIO_NUM;
    config.pin_pclk = PCLK_GPIO_NUM;
    config.pin_vsync = VSYNC_GPIO_NUM;
    config.pin_href = HREF_GPIO_NUM;
    config.pin_sccb_sda = -1;
    config.pin_sccb_scl = -1;
    config.sccb_i2c_port = 0;
    config.pin_pwdn = PWDN_GPIO_NUM;
    config.pin_reset = RESET_GPIO_NUM;
    config.xclk_freq_hz = 20000000;
    config.frame_size = FRAMESIZE_QVGA;      /* 320x240 — small enough to POST */
    config.pixel_format = PIXFORMAT_JPEG;
    config.jpeg_quality = 14;                /* better colour for HSV rust */
    config.fb_count = 2;
    config.grab_mode = CAMERA_GRAB_LATEST;
    config.fb_location = CAMERA_FB_IN_PSRAM;

    esp_err_t err = esp_camera_init(&config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "camera init failed 0x%x", err);
        return false;
    }
    sensor_t *s = esp_camera_sensor_get();
    if (s) {
        s->set_vflip(s, 1);
        s->set_hmirror(s, 0);
        s->set_brightness(s, 1);
        s->set_contrast(s, 1);
        s->set_whitebal(s, 1);
        s->set_awb_gain(s, 1);
        s->set_exposure_ctrl(s, 1);
        s->set_aec2(s, 1);
        s->set_ae_level(s, 1);
        s->set_gain_ctrl(s, 1);
        s->set_gainceiling(s, GAINCEILING_16X);
    }
    ESP_LOGI(TAG, "Camera JPEG QVGA ready for crack detection");
    return true;
}

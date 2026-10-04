#include "bsp_qmi8658.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <math.h>
#include <string.h>

#include "esp_log.h"
#include "esp_err.h"

#include "bsp_i2c.h"

static const char *TAG = "bsp_qmi8658";
#define ACC_SENSITIVITY (4 / 32768.0f) // 每LSB对应的加速度（单位为g）

static i2c_master_dev_handle_t dev_handle;
static i2c_master_bus_handle_t s_bus_handle;
static bool s_added;
static bool s_ready;

static esp_err_t bsp_qmi8658_reg_read(uint8_t reg_addr, uint8_t *data, size_t len);
static esp_err_t bsp_qmi8658_reg_write_byte(uint8_t reg_addr, uint8_t *data, size_t len);

bool bsp_qmi8658_is_ready(void)
{
    return s_ready;
}

static bool qmi_configure(void)
{
    uint8_t id = 0;
    esp_err_t err = ESP_FAIL;
    for (int i = 0; i < 3; i++) {
        id = 0;
        err = bsp_qmi8658_reg_read(QMI8658_WHO_AM_I, &id, 1);
        if (err == ESP_OK && id == 0x05) {
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    if (err != ESP_OK || id != 0x05) {
        s_ready = false;
        ESP_LOGE(TAG, "QMI8658 not found (id=0x%02x err=%s)", id, esp_err_to_name(err));
        return false;
    }
    ESP_LOGI(TAG, "Find QMI8658");
    bsp_qmi8658_reg_write_byte(QMI8658_RESET, (uint8_t[]){0xb0}, 1);
    vTaskDelay(pdMS_TO_TICKS(15));
    bsp_qmi8658_reg_write_byte(QMI8658_CTRL1, (uint8_t[]){0x40}, 1);
    bsp_qmi8658_reg_write_byte(QMI8658_CTRL7, (uint8_t[]){0x03}, 1);
    bsp_qmi8658_reg_write_byte(QMI8658_CTRL2, (uint8_t[]){0x95}, 1);
    bsp_qmi8658_reg_write_byte(QMI8658_CTRL3, (uint8_t[]){0xd5}, 1);
    s_ready = true;
    return true;
}

static esp_err_t bsp_qmi8658_reg_read(uint8_t reg_addr, uint8_t *data, size_t len)
{
    esp_err_t ret = ESP_FAIL;
    if (bsp_i2c_lock(80)) {
        ret = i2c_master_transmit_receive(dev_handle, &reg_addr, 1, data, len, pdMS_TO_TICKS(150));
        bsp_i2c_unlock();
    }
    return ret;
}

static esp_err_t bsp_qmi8658_reg_write_byte(uint8_t reg_addr, uint8_t *data, size_t len)
{
    esp_err_t ret = ESP_FAIL;
    uint8_t buf[len + 1];
    buf[0] = reg_addr;
    memcpy(buf + 1, data, len);

    if (bsp_i2c_lock(80)) {
        ret = i2c_master_transmit(dev_handle, buf, len + 1, pdMS_TO_TICKS(150));
        bsp_i2c_unlock();
    }
    return ret;
}

bool bsp_qmi8658_read_data(qmi8658_data_t *data)
{
    if (!s_ready || data == NULL) {
        return false;
    }
    uint16_t buf[6];
    if (bsp_qmi8658_reg_read(QMI8658_AX_L, (uint8_t *)buf, 12) != ESP_OK) {
        return false;
    }
    data->acc_x = buf[0];
    data->acc_y = buf[1];
    data->acc_z = buf[2];
    data->gyr_x = buf[3];
    data->gyr_y = buf[4];
    data->gyr_z = buf[5];
    data->AngleX = 0;
    data->AngleY = 0;
    data->AngleZ = 0;
    return true;
}

void bsp_qmi8658_init(i2c_master_bus_handle_t bus_handle)
{
    if (s_ready) {
        return;
    }
    if (!s_added) {
        ESP_LOGI(TAG, "QMI8658 Initialize");
    }
    s_bus_handle = bus_handle;
    if (!s_added) {
        vTaskDelay(pdMS_TO_TICKS(20));
        i2c_device_config_t dev_cfg = {};
        dev_cfg.dev_addr_length = I2C_ADDR_BIT_LEN_7;
        dev_cfg.device_address = QMI8658_SENSOR_ADDR;
        dev_cfg.scl_speed_hz = 100000;
        esp_err_t err = i2c_master_bus_add_device(bus_handle, &dev_cfg, &dev_handle);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "add device failed: %s", esp_err_to_name(err));
            return;
        }
        s_added = true;
    }
    (void)qmi_configure();
}

static void qmi8658_test_task(void *arg)
{
    qmi8658_data_t data;
    while (1)
    {
        if (bsp_qmi8658_read_data(&data))
        {
            // printf("Acc: %.2f %.2f %.2f-----Gyr: %04d %04d %04d\n", data.acc_x * ACC_SENSITIVITY * 9.8f, data.acc_y * ACC_SENSITIVITY * 9.8f, data.acc_z * ACC_SENSITIVITY * 9.8f, data.gyr_x, data.gyr_y, data.gyr_z);
            // printf("-------------------------------------------------------------------------\n");
            printf("Angle: %.2f %.2f %.2f\n", data.AngleX, data.AngleY, data.AngleZ);
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

void bsp_qmi8658_test(void)
{
    xTaskCreate(qmi8658_test_task, "qmi8658_test", 4096, NULL, tskIDLE_PRIORITY + 1, NULL);
}
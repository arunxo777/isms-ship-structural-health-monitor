#include "bsp_i2c.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_check.h"
#include "esp_rom_sys.h"
#include "driver/gpio.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

static const char *TAG = "bsp_i2c";

SemaphoreHandle_t  bsp_i2c_mux;

bool bsp_i2c_lock(uint32_t timeout_ms)
{
    assert(bsp_i2c_mux && "lvgl_port_init must be called first");

    const TickType_t timeout_ticks = (timeout_ms == 0) ? portMAX_DELAY : pdMS_TO_TICKS(timeout_ms);
    return xSemaphoreTakeRecursive(bsp_i2c_mux, timeout_ticks) == pdTRUE;
}

void bsp_i2c_unlock(void)
{
    assert(bsp_i2c_mux && "lvgl_port_init must be called first");
    xSemaphoreGiveRecursive(bsp_i2c_mux);
}

void bsp_i2c_recover(i2c_master_bus_handle_t bus_handle)
{
    (void)bus_handle;
}

bool bsp_i2c_lines_idle(void)
{
    return gpio_get_level(EXAMPLE_PIN_I2C_SDA) && gpio_get_level(EXAMPLE_PIN_I2C_SCL);
}

static void i2c_gpio_unstick(void)
{
    gpio_config_t io = {};
    io.pin_bit_mask = (1ULL << EXAMPLE_PIN_I2C_SCL) | (1ULL << EXAMPLE_PIN_I2C_SDA);
    io.mode = GPIO_MODE_INPUT_OUTPUT_OD;
    io.pull_up_en = GPIO_PULLUP_ENABLE;
    io.pull_down_en = GPIO_PULLDOWN_DISABLE;
    io.intr_type = GPIO_INTR_DISABLE;
    gpio_config(&io);

    gpio_set_level(EXAMPLE_PIN_I2C_SCL, 1);
    gpio_set_level(EXAMPLE_PIN_I2C_SDA, 1);
    vTaskDelay(pdMS_TO_TICKS(5));

    for (int burst = 0; burst < 24; burst++) {
        if (gpio_get_level(EXAMPLE_PIN_I2C_SDA) && gpio_get_level(EXAMPLE_PIN_I2C_SCL)) {
            break;
        }
        for (int i = 0; i < 9; i++) {
            gpio_set_level(EXAMPLE_PIN_I2C_SCL, 0);
            esp_rom_delay_us(10);
            gpio_set_level(EXAMPLE_PIN_I2C_SCL, 1);
            esp_rom_delay_us(10);
        }
        gpio_set_level(EXAMPLE_PIN_I2C_SDA, 0);
        esp_rom_delay_us(10);
        gpio_set_level(EXAMPLE_PIN_I2C_SCL, 1);
        esp_rom_delay_us(10);
        gpio_set_level(EXAMPLE_PIN_I2C_SDA, 1);
        vTaskDelay(pdMS_TO_TICKS(10));
    }

    ESP_LOGI(TAG, "I2C GPIO unstick  SDA=%d SCL=%d",
             gpio_get_level(EXAMPLE_PIN_I2C_SDA),
             gpio_get_level(EXAMPLE_PIN_I2C_SCL));
}

i2c_master_bus_handle_t bsp_i2c_init(void)
{
    /* AXP2101 pull-ups need a moment after USB reset. */
    vTaskDelay(pdMS_TO_TICKS(250));

    for (int i = 0; i < 12; i++) {
        i2c_gpio_unstick();
        if (gpio_get_level(EXAMPLE_PIN_I2C_SDA) && gpio_get_level(EXAMPLE_PIN_I2C_SCL)) {
            break;
        }
        ESP_LOGW(TAG, "I2C lines still low (%d/12) — press the PWR button if the board is off", i + 1);
        vTaskDelay(pdMS_TO_TICKS(150));
    }

    i2c_master_bus_handle_t i2c_bus_handle;
    i2c_master_bus_config_t i2c_mst_config = {};
    i2c_mst_config.clk_source = I2C_CLK_SRC_DEFAULT;
    i2c_mst_config.i2c_port = (i2c_port_num_t)I2C_PORT_NUM;
    i2c_mst_config.scl_io_num = EXAMPLE_PIN_I2C_SCL;
    i2c_mst_config.sda_io_num = EXAMPLE_PIN_I2C_SDA;
    i2c_mst_config.glitch_ignore_cnt = 7;
    i2c_mst_config.flags.enable_internal_pullup = 1;

    ESP_ERROR_CHECK(i2c_new_master_bus(&i2c_mst_config, &i2c_bus_handle));

    bsp_i2c_mux = xSemaphoreCreateRecursiveMutex();
    return i2c_bus_handle;
}

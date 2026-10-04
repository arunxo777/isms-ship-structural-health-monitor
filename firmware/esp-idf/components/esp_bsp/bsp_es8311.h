#pragma once

#include <stdio.h>
#include <stdbool.h>
#include "driver/i2c_master.h"

#ifdef __cplusplus
extern "C" {
#endif

void bsp_es8311_init(i2c_master_bus_handle_t bus_handle);
void bsp_es8311_test(void);

void bsp_es8311_recording(uint8_t *data, size_t limit_size);
void bsp_es8311_playing(uint8_t *data, size_t limit_size);

/* Continuous mic capture used by ISMS — does not reconfigure I2S pins. */
void bsp_es8311_enable_mic(void);
int bsp_es8311_read_pcm(void *data, size_t bytes);
bool bsp_es8311_is_ready(void);


#ifdef __cplusplus
}
#endif



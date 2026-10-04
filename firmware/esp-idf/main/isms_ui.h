#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    float accel_x_g;
    float accel_y_g;
    float accel_z_g;
    float accel_mag_g;
    float gyro_x_dps;
    float gyro_y_dps;
    float gyro_z_dps;
    float angle_x;
    float angle_y;
    float angle_z;
    float acoustic_rms;
    float acoustic_peak_hz;
    bool imu_ok;
    bool imu_calibrated;
    bool still;
    int cal_pct;
    bool mic_ok;
    bool mic_calibrated;
} isms_ui_snapshot_t;

void isms_ui_init(void);
void isms_ui_update(const isms_ui_snapshot_t *snap);

#ifdef __cplusplus
}
#endif

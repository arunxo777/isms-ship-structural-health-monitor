/*
 * ISMS calibrated dashboard. Hold lv_port lock when calling.
 */

#include "isms_ui.h"
#include "lvgl.h"
#include "lv_port.h"

#include <stdio.h>
#include <string.h>

static lv_obj_t *s_lbl_title;
static lv_obj_t *s_lbl_state;
static lv_obj_t *s_status_box;
static lv_obj_t *s_lbl_status;
static lv_obj_t *s_lbl_mag;
static lv_obj_t *s_lbl_xyz;
static lv_obj_t *s_lbl_gyro;
static lv_obj_t *s_lbl_ang;
static lv_obj_t *s_lbl_mic;
static lv_obj_t *s_lbl_hint;

static void set_text_if_changed(lv_obj_t *lbl, const char *txt)
{
    const char *cur = lv_label_get_text(lbl);
    if (cur != NULL && strcmp(cur, txt) == 0) {
        return;
    }
    lv_label_set_text(lbl, txt);
}

static lv_obj_t *make_label(lv_obj_t *parent, const lv_font_t *font, lv_color_t color)
{
    lv_obj_t *lbl = lv_label_create(parent);
    lv_obj_set_style_text_font(lbl, font, 0);
    lv_obj_set_style_text_color(lbl, color, 0);
    lv_label_set_text(lbl, "");
    return lbl;
}

static lv_obj_t *make_card(lv_obj_t *scr, int y, int h)
{
    lv_obj_t *card = lv_obj_create(scr);
    lv_obj_set_size(card, 304, h);
    lv_obj_set_pos(card, 8, y);
    lv_obj_set_style_bg_color(card, lv_color_hex(0x122038), 0);
    lv_obj_set_style_border_width(card, 0, 0);
    lv_obj_set_style_radius(card, 8, 0);
    lv_obj_set_style_pad_all(card, 8, 0);
    lv_obj_clear_flag(card, LV_OBJ_FLAG_SCROLLABLE);
    return card;
}

void isms_ui_init(void)
{
    lv_obj_t *scr = lv_scr_act();
    lv_obj_set_style_bg_color(scr, lv_color_hex(0x0A1628), 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
    lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *top = lv_obj_create(scr);
    lv_obj_set_size(top, 320, 36);
    lv_obj_set_pos(top, 0, 0);
    lv_obj_set_style_radius(top, 0, 0);
    lv_obj_set_style_border_width(top, 0, 0);
    lv_obj_set_style_bg_color(top, lv_color_hex(0x07101C), 0);
    lv_obj_set_style_pad_all(top, 4, 0);
    lv_obj_clear_flag(top, LV_OBJ_FLAG_SCROLLABLE);

    s_lbl_title = make_label(top, &lv_font_montserrat_16, lv_color_hex(0xFFFFFF));
    lv_label_set_text(s_lbl_title, "ISMS  Hull node");
    lv_obj_align(s_lbl_title, LV_ALIGN_LEFT_MID, 4, 0);

    s_lbl_state = make_label(top, &lv_font_montserrat_12, lv_color_hex(0xF1C40F));
    lv_label_set_text(s_lbl_state, "CAL");
    lv_obj_align(s_lbl_state, LV_ALIGN_RIGHT_MID, -6, 0);

    s_status_box = make_card(scr, 42, 88);
    s_lbl_status = make_label(s_status_box, &lv_font_montserrat_20, lv_color_hex(0xFFFFFF));
    lv_label_set_text(s_lbl_status, "CALIBRATING");
    lv_obj_align(s_lbl_status, LV_ALIGN_TOP_MID, 0, 2);
    s_lbl_mag = make_label(s_status_box, &lv_font_montserrat_16, lv_color_hex(0xF0F0F0));
    lv_label_set_text(s_lbl_mag, "|a| = -- g");
    lv_obj_align(s_lbl_mag, LV_ALIGN_CENTER, 0, 12);
    s_lbl_hint = make_label(s_status_box, &lv_font_montserrat_12, lv_color_hex(0xA8B4C8));
    lv_label_set_text(s_lbl_hint, "Hold the board still");
    lv_obj_align(s_lbl_hint, LV_ALIGN_BOTTOM_MID, 0, -2);

    lv_obj_t *acc = make_card(scr, 138, 92);
    s_lbl_xyz = make_label(acc, &lv_font_montserrat_16, lv_color_hex(0xFFFFFF));
    lv_label_set_long_mode(s_lbl_xyz, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(s_lbl_xyz, 288);
    lv_label_set_text(s_lbl_xyz, "Accel X  -- g\nAccel Y  -- g\nAccel Z  -- g");

    lv_obj_t *gyr = make_card(scr, 238, 92);
    s_lbl_gyro = make_label(gyr, &lv_font_montserrat_16, lv_color_hex(0xFFFFFF));
    lv_label_set_long_mode(s_lbl_gyro, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(s_lbl_gyro, 288);
    lv_label_set_text(s_lbl_gyro, "Gyro X  -- dps\nGyro Y  -- dps\nGyro Z  -- dps");

    lv_obj_t *ang = make_card(scr, 338, 88);
    s_lbl_ang = make_label(ang, &lv_font_montserrat_16, lv_color_hex(0xFFFFFF));
    lv_label_set_long_mode(s_lbl_ang, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(s_lbl_ang, 288);
    lv_label_set_text(s_lbl_ang, "Angle X  -- deg\nAngle Y  -- deg\nAngle Z  -- deg");

    lv_obj_t *bot = lv_obj_create(scr);
    lv_obj_set_size(bot, 320, 46);
    lv_obj_set_pos(bot, 0, 434);
    lv_obj_set_style_radius(bot, 0, 0);
    lv_obj_set_style_border_width(bot, 0, 0);
    lv_obj_set_style_bg_color(bot, lv_color_hex(0x07101C), 0);
    lv_obj_set_style_pad_all(bot, 4, 0);
    lv_obj_clear_flag(bot, LV_OBJ_FLAG_SCROLLABLE);
    s_lbl_mic = make_label(bot, &lv_font_montserrat_12, lv_color_hex(0xC0C8D8));
    lv_label_set_text(s_lbl_mic, "Mic --");
    lv_obj_align(s_lbl_mic, LV_ALIGN_LEFT_MID, 6, 0);
}

void isms_ui_update(const isms_ui_snapshot_t *snap)
{
    if (snap == NULL) {
        return;
    }

    static int last_mode = -1;
    char buf[160];
    int mode;
    /* A missed sample is not a fault. Only show FAULT if the chip never came up. */
    if (!snap->imu_ok && !snap->imu_calibrated) {
        mode = 1;
    } else if (!snap->imu_calibrated) {
        mode = 1;
    } else if (snap->still) {
        mode = 2;
    } else {
        mode = 3;
    }

    if (mode != last_mode) {
        last_mode = mode;
        if (mode == 1) {
            lv_label_set_text(s_lbl_state, "CAL");
            lv_obj_set_style_text_color(s_lbl_state, lv_color_hex(0xF1C40F), 0);
            lv_obj_set_style_bg_color(s_status_box, lv_color_hex(0x1A4A7A), 0);
            lv_label_set_text(s_lbl_hint, "Hold still 4 seconds");
        } else if (mode == 2) {
            lv_label_set_text(s_lbl_state, "STILL");
            lv_obj_set_style_text_color(s_lbl_state, lv_color_hex(0x2ECC71), 0);
            lv_obj_set_style_bg_color(s_status_box, lv_color_hex(0x1B7A3A), 0);
            lv_label_set_text(s_lbl_status, "CALIBRATED");
            lv_label_set_text(s_lbl_hint, "Held while still");
        } else {
            lv_label_set_text(s_lbl_state, "MOVE");
            lv_obj_set_style_text_color(s_lbl_state, lv_color_hex(0xF1C40F), 0);
            lv_obj_set_style_bg_color(s_status_box, lv_color_hex(0x8A6D12), 0);
            lv_label_set_text(s_lbl_status, "MOTION");
            lv_label_set_text(s_lbl_hint, "Live after calibration");
        }
    }

    if (mode == 1) {
        snprintf(buf, sizeof(buf), "CALIBRATING %d%%", snap->cal_pct);
        set_text_if_changed(s_lbl_status, buf);
    }

    snprintf(buf, sizeof(buf), "|a| = %.2f g", snap->accel_mag_g);
    set_text_if_changed(s_lbl_mag, buf);

    snprintf(buf, sizeof(buf),
             "Accel X   %+.2f g\nAccel Y   %+.2f g\nAccel Z   %+.2f g",
             snap->accel_x_g, snap->accel_y_g, snap->accel_z_g);
    set_text_if_changed(s_lbl_xyz, buf);

    snprintf(buf, sizeof(buf),
             "Gyro X    %+.1f dps\nGyro Y    %+.1f dps\nGyro Z    %+.1f dps",
             snap->gyro_x_dps, snap->gyro_y_dps, snap->gyro_z_dps);
    set_text_if_changed(s_lbl_gyro, buf);

    snprintf(buf, sizeof(buf),
             "Angle X   %+.1f deg\nAngle Y   %+.1f deg\nAngle Z   %+.1f deg",
             snap->angle_x, snap->angle_y, snap->angle_z);
    set_text_if_changed(s_lbl_ang, buf);

    if (!snap->mic_ok) {
        snprintf(buf, sizeof(buf), "Mic starting");
    } else if (!snap->mic_calibrated) {
        snprintf(buf, sizeof(buf), "Mic CAL  %.3f", snap->acoustic_rms);
    } else {
        int pct = (int)(snap->acoustic_rms * 400.0f);
        if (pct < 0) {
            pct = 0;
        } else if (pct > 100) {
            pct = 100;
        }
        snprintf(buf, sizeof(buf), "Mic LIVE  %d%%   %.3f   %.0f Hz",
                 pct, snap->acoustic_rms, snap->acoustic_peak_hz);
    }
    set_text_if_changed(s_lbl_mic, buf);
}

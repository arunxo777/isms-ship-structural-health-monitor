idf_component_register(SRCS "main.cpp" "isms_ui.cpp" "isms_camera.c" "drawing_screen.c"
                    INCLUDE_DIRS "."
                    REQUIRES lvgl lvgl_ui esp_bsp esp_lv_port esp32-camera
                    PRIV_REQUIRES
                        esp_http_client
                        json
                        spiffs
                        esp_wifi
                        nvs_flash
                        esp_timer
                        driver
                        vfs)

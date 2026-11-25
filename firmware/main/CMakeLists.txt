idf_component_register(
    SRCS "main.c" "board.c" "display.c" "net.c" "hub_server.c" "weather.c"
         "ui.c" "ui_home.c" "ui_stats.c" "ui_media.c" "ui_calendar.c" "ui_weather.c" "ui_shortcuts.c" "ui_claude.c" "ui_printer.c" "ui_alert.c" "ui_timer.c" "ui_games.c" "ui_net.c" "ui_github.c" "ui_saver.c" "ui_update.c" "ui_dash.c" "ota.c" "logos.c" "lv_mem_psram.c"
    INCLUDE_DIRS "."
    EMBED_FILES "fonts/MaterialIcons-Regular.ttf" "fonts/Inter-Medium.ttf" "fonts/Inter-SemiBold.ttf" "fonts/Inter-SemiBold-latin-ext.ttf" "fonts/Inter-SemiBold-tnum.ttf"
    REQUIRES esp_lcd esp_driver_i2c esp_driver_gpio esp_wifi esp_netif esp_event nvs_flash
             esp_http_server esp_http_client esp-tls json esp_timer app_update esp_driver_uart
)

# LVGL calls lv_malloc_core() & co. from lv_mem_psram.c; nothing in main refers
# to them, so make the linker keep that object.
target_link_libraries(${COMPONENT_LIB} INTERFACE "-u lv_malloc_core")

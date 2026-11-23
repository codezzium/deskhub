// Material Icons code points (fonts/MaterialIcons-Regular.ttf, Apache 2.0).
// Each glyph is a UTF-8 string so it can go straight into lv_label_set_text.
#pragma once

#include <stdint.h>

#define ICON_HOME               "\xEE\xA2\x8A" /* e88a */
#define ICON_INSIGHTS           "\xEF\x82\x92" /* f092 */
#define ICON_MEMORY             "\xEE\x8C\xA2" /* e322 */
#define ICON_SYSTEM_UPDATE      "\xEE\x98\xAA" /* e62a */
#define ICON_SPEED              "\xEE\xA7\xA4" /* e9e4 */
#define ICON_MUSIC_NOTE         "\xEE\x90\x85" /* e405 */
#define ICON_MOVIE              "\xEE\x80\xAC" /* e02c */
#define ICON_EVENT              "\xEE\xA1\xB8" /* e878 */
#define ICON_CALENDAR_TODAY     "\xEE\xA4\xB5" /* e935 */
#define ICON_WB_SUNNY           "\xEE\x90\xB0" /* e430 */
#define ICON_CLOUD              "\xEE\x8A\xBD" /* e2bd */
#define ICON_WB_CLOUDY          "\xEE\x90\xAD" /* e42d */
#define ICON_GRAIN              "\xEE\x8F\xAA" /* e3ea */
#define ICON_UMBRELLA           "\xEF\x86\xAD" /* f1ad */
#define ICON_AC_UNIT            "\xEE\xAC\xBB" /* eb3b */
#define ICON_THUNDERSTORM       "\xEE\xAF\x9B" /* ebdb */
#define ICON_FOGGY              "\xEE\xA0\x98" /* e818 */
#define ICON_NIGHTS_STAY        "\xEE\xA9\x86" /* ea46 */
#define ICON_AIR                "\xEE\xBF\x98" /* efd8 */
#define ICON_WATER_DROP         "\xEE\x9E\x98" /* e798 */
#define ICON_GRID_VIEW          "\xEE\xA6\xB0" /* e9b0 */
#define ICON_WIFI               "\xEE\x98\xBE" /* e63e */
#define ICON_WIFI_OFF           "\xEE\x99\x88" /* e648 */
#define ICON_DESKTOP_WINDOWS    "\xEE\x8C\x8C" /* e30c */
#define ICON_PLAY_ARROW         "\xEE\x80\xB7" /* e037 */
#define ICON_PAUSE              "\xEE\x80\xB4" /* e034 */
#define ICON_SKIP_NEXT          "\xEE\x81\x84" /* e044 */
#define ICON_SKIP_PREVIOUS      "\xEE\x81\x85" /* e045 */
#define ICON_THERMOSTAT         "\xEF\x81\xB6" /* f076 */
#define ICON_LOCATION_ON        "\xEE\x83\x88" /* e0c8 */
#define ICON_ARROW_UPWARD       "\xEE\x97\x98" /* e5d8 */
#define ICON_ARROW_DOWNWARD     "\xEE\x97\x9B" /* e5db */
#define ICON_AUTO_AWESOME       "\xEE\x99\x9F" /* e65f */
#define ICON_HOURGLASS_BOTTOM   "\xEE\xA9\x9C" /* ea5c */
#define ICON_VIEW_IN_AR          "\xEE\xA7\xBE" /* e9fe */
#define ICON_LAYERS              "\xEE\x94\xBB" /* e53b */
#define ICON_LOCAL_FIRE          "\xEE\xBD\x95" /* ef55 */
#define ICON_DEVICE_THERMOSTAT   "\xEE\x87\xBF" /* e1ff */
#define ICON_ERROR               "\xEE\x80\x80" /* e000 */
#define ICON_STORAGE           "\xEE\x87\x9B" /* e1db */

typedef struct {
    const char *name;
    uint16_t cp;
} hub_icon_t;

// Names the companion may use for shortcut tiles.
static const hub_icon_t HUB_ICONS[] = {
    {"home", 0xe88a}, {"insights", 0xf092}, {"memory", 0xe322}, {"speed", 0xe9e4},
    {"music_note", 0xe405}, {"event", 0xe878}, {"calendar_today", 0xe935},
    {"wb_sunny", 0xe430}, {"cloud", 0xe2bd}, {"grid_view", 0xe9b0}, {"apps", 0xe5c3},
    {"wifi", 0xe63e}, {"computer", 0xe30a}, {"desktop_windows", 0xe30c},
    {"play_arrow", 0xe037}, {"pause", 0xe034}, {"skip_next", 0xe044},
    {"skip_previous", 0xe045}, {"volume_up", 0xe050}, {"volume_down", 0xe04d},
    {"volume_off", 0xe04f}, {"mic", 0xe029}, {"mic_off", 0xe02b}, {"lock", 0xe897},
    {"photo_camera", 0xe412}, {"screenshot_monitor", 0xec08}, {"terminal", 0xeb8e},
    {"code", 0xe86f}, {"public", 0xe80b}, {"folder", 0xe2c7}, {"keyboard", 0xe312},
    {"power_settings_new", 0xe8ac}, {"schedule", 0xe8b5}, {"timer", 0xe425},
    {"mail", 0xe158}, {"chat", 0xe0b7}, {"sports_esports", 0xea28},
    {"rocket_launch", 0xeb9b}, {"bolt", 0xea0b}, {"star", 0xe838}, {"favorite", 0xe87d},
    {"settings", 0xe8b8}, {"refresh", 0xe5d5}, {"dark_mode", 0xe51c},
    {"headphones", 0xf01f}, {"videocam", 0xe04b}, {"movie", 0xe02c},
    {"developer_board", 0xe30d}, {"lan", 0xeb2f}, {"shuffle", 0xe043}, {"repeat", 0xe040},
};
#define ICON_TIMER             "\xEE\x90\xA5" /* e425 */
#define ICON_REPLAY            "\xEE\x81\x82" /* e042 */
#define ICON_CLOSE             "\xEE\x97\x8D" /* e5cd */
#define ICON_OPEN_IN_NEW       "\xEE\xA2\x9E" /* e89e */
#define ICON_SPORTS_ESPORTS    "\xEE\xA8\xA8" /* ea28 */
#define ICON_PUBLIC            "\xEE\xA0\x8B" /* e80b */
#define ICON_COMMIT            "\xEE\xAB\xB5" /* eaf5 */
#define ICON_REFRESH          "\xEE\x97\x95" /* e5d5 */

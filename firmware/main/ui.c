#include "ui.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_system.h"
#include "hub_server.h"
#include "icons.h"
#include "logos.h"
#include "net.h"
#include "nvs.h"
#include "sdkconfig.h"
#include "ui_internal.h"
#include "src/widgets/label/lv_label_private.h"   // the text offset, for ui_marquee()

static const char *TAG = "ui";

extern const uint8_t icons_ttf_start[] asm("_binary_MaterialIcons_Regular_ttf_start");
extern const uint8_t icons_ttf_end[] asm("_binary_MaterialIcons_Regular_ttf_end");
// Inter: Medium for text, SemiBold for large numbers and the clock.
extern const uint8_t text_ttf_start[] asm("_binary_Inter_Medium_ttf_start");
extern const uint8_t text_ttf_end[] asm("_binary_Inter_Medium_ttf_end");
extern const uint8_t clock_ttf_start[] asm("_binary_Inter_SemiBold_ttf_start");
extern const uint8_t clock_ttf_end[] asm("_binary_Inter_SemiBold_ttf_end");

lv_font_t *g_icon_20, *g_icon_24, *g_icon_32, *g_icon_48, *g_icon_80, *g_font_clock;
lv_font_t *g_font_14, *g_font_16, *g_font_20, *g_font_24, *g_font_28, *g_font_32, *g_font_48;

// Inter at one size. Glyphs Inter lacks come from the built-in Montserrat of
// that size, which is also used outright if the TTF can't be loaded.
static lv_font_t *inter(int px, bool semibold, const lv_font_t *builtin)
{
    const uint8_t *start = semibold ? clock_ttf_start : text_ttf_start;
    const uint8_t *end = semibold ? clock_ttf_end : text_ttf_end;
    // No kerning: stb_truetype looks each letter pair up in the font's GPOS table
    // on every layout and draw, which made full redraws several times slower.
    lv_font_t *f = lv_tiny_ttf_create_data_ex(start, end - start, px, LV_FONT_KERNING_NONE, 256);
    if (!f) {
        ESP_LOGE(TAG, "Inter %d px failed, using built-in font", px);
        return (lv_font_t *)builtin;
    }
    f->fallback = builtin;
    return f;
}

hub_stats_t g_stats;
hub_media_t g_media;
hub_weather_t g_weather;
hub_event_t g_events[HUB_MAX_EVENTS];
int g_event_count;
int g_event_days = 7;
lv_image_dsc_t g_art;
uint32_t g_art_id;
hub_server_t g_server;
lv_image_dsc_t g_srv_art;
uint32_t g_srv_art_id;
static uint8_t *s_srv_art_buf;
bool g_pc_online;

static uint8_t *s_art_buf;
static lv_obj_t *s_pages[PAGE_COUNT];
static lv_obj_t *s_nav[PAGE_COUNT];
static lv_obj_t *s_wifi_icon, *s_pc_icon;
static lv_obj_t *s_nav_logo;   // the AI tab's agent logo
int g_nav = NAV_LEFT;

#define RAIL_PAD_V 10
#define RAIL_GAP 2
#define RAIL_TAB_MAX 52
static int s_page = -1;

// Default until the companion says otherwise: everything but the printer page.
uint32_t g_pages_mask = ((1u << PAGE_COUNT) - 1) & ~(1u << PAGE_PRINTER) & ~(1u << PAGE_GITHUB);
hub_printer_t g_printers[HUB_MAX_PRINTERS];
int g_printer_count;

// The clock's TTF at any pixel size (one extra size is kept besides the 128 px font).
// The previous extra size is returned in *old so the caller can free it once no
// label uses it any more.
lv_font_t *ui_clock_font(int px, lv_font_t **old)
{
    static lv_font_t *s_font;
    static int s_px;
    *old = NULL;
    if (px == 128 || px < 16) return g_font_clock;
    if (s_font && s_px == px) return s_font;
    lv_font_t *f = lv_tiny_ttf_create_data(clock_ttf_start, clock_ttf_end - clock_ttf_start, px);
    if (!f) return g_font_clock;
    *old = s_font;
    s_font = f;
    s_px = px;
    return f;
}

// The clock's TTF at 40 px, for Home's slim top bar. Created once on first use.
lv_font_t *ui_header_font(void)
{
    static lv_font_t *s_font;
    if (!s_font) s_font = lv_tiny_ttf_create_data(clock_ttf_start, clock_ttf_end - clock_ttf_start, 40);
    return s_font ? s_font : (lv_font_t *)g_font_32;
}

// The clock's TTF at 72 px, for the timer's countdown. Created once on first use.
lv_font_t *ui_timer_font(void)
{
    static lv_font_t *s_font;
    if (!s_font) s_font = lv_tiny_ttf_create_data(clock_ttf_start, clock_ttf_end - clock_ttf_start, 72);
    return s_font ? s_font : (lv_font_t *)g_font_48;
}

// Same TTF as the clock, for the date line (its own cached size).
lv_font_t *ui_date_font(int px, lv_font_t **old)
{
    static lv_font_t *s_font;
    static int s_px;
    *old = NULL;
    if (px < 12) px = 12;
    if (s_font && s_px == px) return s_font;
    lv_font_t *f = lv_tiny_ttf_create_data(clock_ttf_start, clock_ttf_end - clock_ttf_start, px);
    if (!f) return NULL;
    *old = s_font;
    s_font = f;
    s_px = px;
    return f;
}

// The AI agent whose usage the AI page shows (the companion's "provider").
// To add an agent: its logo in tools/make_logos.py, then a case here.
static bool agent_is_codex(void)
{
    return strcmp(g_claude.provider, "codex") == 0;
}

const lv_image_dsc_t *ui_agent_logo(int size)
{
    if (agent_is_codex()) return size >= 32 ? &logo_codex_32 : &logo_codex_24;
    return size >= 32 ? &logo_claude_32 : &logo_claude_24;
}

const char *ui_agent_name(void)
{
    return agent_is_codex() ? "Codex" : "Claude Code";
}

uint32_t ui_agent_color(void)
{
    return agent_is_codex() ? 0x10a37f : 0xd97757;
}

// ---- shared helpers --------------------------------------------------------

lv_obj_t *ui_card(lv_obj_t *parent)
{
    lv_obj_t *c = lv_obj_create(parent);
    lv_obj_remove_style_all(c);
    // Flat card with a hairline edge.
    lv_obj_set_style_bg_color(c, lv_color_hex(COL_CARD), 0);
    lv_obj_set_style_bg_opa(c, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(c, lv_color_hex(COL_CARD_HI), LV_STATE_PRESSED);
    lv_obj_set_style_border_color(c, lv_color_hex(COL_BORDER), 0);
    lv_obj_set_style_border_width(c, 1, 0);
    lv_obj_set_style_radius(c, 16, 0);
    lv_obj_set_style_pad_all(c, 16, 0);
    lv_obj_remove_flag(c, LV_OBJ_FLAG_SCROLLABLE);
    return c;
}

// ---- marquee: readable scrolling for text that doesn't fit -------------------
// The label clips its text, and our own animation moves the text inside it:
// rest on the start, slide (easing in and out) until the end shows, rest, slide
// back, repeat, at about 30 px/s whatever the distance. Text that fits doesn't
// move. LVGL's own scroll mode isn't used because it restarts whenever the text
// is set again, even to the same string, and caps a sweep at 10 s.
#define MARQUEE_REST_MS 2500
#define MARQUEE_PX_PER_S 30

typedef struct {
    uint32_t hash;   // text the animation was made for
    int32_t width;   // and the label's width then
} marquee_t;

static uint32_t text_hash(const char *s)
{
    uint32_t h = 2166136261u;
    while (*s) h = (h ^ (uint8_t)*s++) * 16777619u;
    return h;
}

static void marquee_exec(void *obj, int32_t v)
{
    lv_label_t *l = obj;
    if (l->offset.x == v) return;
    l->offset.x = v;
    lv_obj_invalidate(obj);
}

// Runs just before the label draws: (re)starts the motion when its text or
// width changed. Nothing here may invalidate, so the offset is set directly.
static void marquee_draw_cb(lv_event_t *e)
{
    lv_obj_t *obj = lv_event_get_target(e);
    lv_label_t *l = (lv_label_t *)obj;
    marquee_t *m = lv_event_get_user_data(e);
    const char *text = lv_label_get_text(obj);
    uint32_t h = text_hash(text);
    int32_t w = lv_obj_get_content_width(obj);
    if (h == m->hash && w == m->width) return;
    m->hash = h;
    m->width = w;
    lv_anim_delete(obj, marquee_exec);
    l->offset.x = 0;
    lv_point_t size;
    lv_text_get_size(&size, text, lv_obj_get_style_text_font(obj, 0),
                     lv_obj_get_style_text_letter_space(obj, 0), 0, LV_COORD_MAX, LV_TEXT_FLAG_NONE);
    int32_t over = size.x - w;
    if (over <= 0) return;
    uint32_t ms = LV_CLAMP(1500, (uint32_t)over * 1000 / MARQUEE_PX_PER_S, 30000);
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, obj);
    lv_anim_set_exec_cb(&a, marquee_exec);
    lv_anim_set_values(&a, 0, -over);
    lv_anim_set_path_cb(&a, lv_anim_path_ease_in_out);
    lv_anim_set_duration(&a, ms);
    lv_anim_set_playback_duration(&a, ms);
    lv_anim_set_delay(&a, MARQUEE_REST_MS);           // rest on the start
    lv_anim_set_playback_delay(&a, MARQUEE_REST_MS);  // rest at the end
    lv_anim_set_repeat_delay(&a, MARQUEE_REST_MS);    // rest back at the start
    lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
    lv_anim_set_early_apply(&a, false);
    lv_anim_start(&a);
}

static void marquee_delete_cb(lv_event_t *e)
{
    lv_anim_delete(lv_event_get_target(e), marquee_exec);
    lv_free(lv_event_get_user_data(e));
}

void ui_marquee(lv_obj_t *label)
{
    lv_label_set_long_mode(label, LV_LABEL_LONG_CLIP);
    marquee_t *m = lv_malloc_zeroed(sizeof(marquee_t));
    if (!m) return;
    m->width = -1;
    lv_obj_add_event_cb(label, marquee_draw_cb, LV_EVENT_DRAW_MAIN_BEGIN, m);
    lv_obj_add_event_cb(label, marquee_delete_cb, LV_EVENT_DELETE, m);
}

void ui_spread(lv_obj_t *card, lv_obj_t *const *rows, int n)
{
    lv_obj_update_layout(card);
    int sum = 0;
    for (int i = 0; i < n; i++) sum += lv_obj_get_height(rows[i]);
    int gap = n > 1 ? (lv_obj_get_content_height(card) - sum) / (n - 1) : 0;
    if (gap < 0) gap = 0;
    for (int i = 0, y = 0; i < n; i++) {
        lv_obj_set_y(rows[i], y);
        y += lv_obj_get_height(rows[i]) + gap;
    }
}

lv_obj_t *ui_caption(lv_obj_t *parent, const char *text)
{
    lv_obj_t *l = ui_label(parent, g_font_14, COL_MUTED, text);
    lv_obj_set_style_text_letter_space(l, 2, 0);
    return l;
}

void ui_bar_set_color(lv_obj_t *bar, uint32_t color)
{
    lv_obj_set_style_bg_color(bar, lv_color_hex(color), LV_PART_INDICATOR);
}

lv_obj_t *ui_label(lv_obj_t *parent, const lv_font_t *font, uint32_t color, const char *text)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(color), 0);
    lv_label_set_text(l, text ? text : "");
    return l;
}

lv_obj_t *ui_bar(lv_obj_t *parent, uint32_t color)
{
    lv_obj_t *b = lv_bar_create(parent);
    lv_bar_set_range(b, 0, 100);
    lv_obj_set_height(b, 8);
    lv_obj_set_style_bg_color(b, lv_color_hex(COL_CARD_HI), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(b, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_radius(b, LV_RADIUS_CIRCLE, LV_PART_MAIN);
    lv_obj_set_style_radius(b, LV_RADIUS_CIRCLE, LV_PART_INDICATOR);
    ui_bar_set_color(b, color);
    return b;
}

lv_obj_t *ui_round_button(lv_obj_t *parent, int size, uint32_t bg, const lv_font_t *icon_font,
                          const char *glyph, lv_event_cb_t cb, void *user)
{
    lv_obj_t *b = lv_button_create(parent);
    lv_obj_remove_style_all(b);
    lv_obj_set_size(b, size, size);
    lv_obj_set_style_radius(b, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(b, lv_color_hex(bg), 0);
    lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
    lv_obj_set_style_border_color(b, lv_color_hex(COL_BORDER), 0);
    lv_obj_set_style_border_width(b, bg == COL_CARD ? 1 : 0, 0);
    bool accent = bg == COL_ACCENT;
    lv_obj_set_style_bg_color(b, lv_color_hex(accent ? COL_ACCENT_PRESSED : COL_PRESSED), LV_STATE_PRESSED);
    lv_obj_set_style_transform_scale(b, 236, LV_STATE_PRESSED);
    lv_obj_set_style_transform_pivot_x(b, size / 2, 0);
    lv_obj_set_style_transform_pivot_y(b, size / 2, 0);
    lv_obj_set_ext_click_area(b, 8);
    lv_obj_t *l = ui_label(b, icon_font, accent ? COL_ON_ACCENT : COL_TEXT, glyph);
    lv_obj_center(l);
    if (cb) lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, user);
    return b;
}

void ui_fmt_clock(char *buf, size_t n, time_t t)
{
    struct tm tm;
    localtime_r(&t, &tm);
#if CONFIG_HUB_24H
    snprintf(buf, n, "%02d:%02d", tm.tm_hour, tm.tm_min);
#else
    int h = tm.tm_hour % 12;
    snprintf(buf, n, "%d:%02d %s", h ? h : 12, tm.tm_min, tm.tm_hour < 12 ? "AM" : "PM");
#endif
}

bool ui_limits_stale(time_t updated)
{
    return updated > 0 && time(NULL) - updated >= 10 * 60;
}

void ui_fmt_asof(char *buf, size_t n, time_t updated)
{
    char t[16];
    if (time(NULL) - updated < 20 * 3600) {
        ui_fmt_clock(t, sizeof(t), updated);
        snprintf(buf, n, "as of %s", t);
    } else {
        struct tm tm;
        localtime_r(&updated, &tm);
        strftime(buf, n, "as of %a", &tm);
    }
}

void ui_fmt_hour(char *buf, size_t n, time_t t)
{
    struct tm tm;
    localtime_r(&t, &tm);
#if CONFIG_HUB_24H
    snprintf(buf, n, "%02d:00", tm.tm_hour);
#else
    int h = tm.tm_hour % 12;
    snprintf(buf, n, "%d %s", h ? h : 12, tm.tm_hour < 12 ? "AM" : "PM");
#endif
}

void ui_fmt_temp(char *buf, size_t n, float t)
{
    snprintf(buf, n, "%d\xC2\xB0", (int)(t < 0 ? t - 0.5f : t + 0.5f));
}

void ui_fmt_duration(char *buf, size_t n, int s)
{
    if (s < 0) s = 0;
    if (s >= 3600) snprintf(buf, n, "%d:%02d:%02d", s / 3600, (s / 60) % 60, s % 60);
    else snprintf(buf, n, "%d:%02d", s / 60, s % 60);
}

void ui_utf8_glyph(char out[5], uint32_t cp)
{
    if (cp < 0x80) {
        out[0] = cp; out[1] = 0;
    } else if (cp < 0x800) {
        out[0] = 0xC0 | (cp >> 6); out[1] = 0x80 | (cp & 0x3F); out[2] = 0;
    } else {
        out[0] = 0xE0 | (cp >> 12); out[1] = 0x80 | ((cp >> 6) & 0x3F);
        out[2] = 0x80 | (cp & 0x3F); out[3] = 0;
    }
}

const char *ui_icon_by_name(const char *name, char out[5])
{
    for (size_t i = 0; i < sizeof(HUB_ICONS) / sizeof(HUB_ICONS[0]); i++) {
        if (strcmp(HUB_ICONS[i].name, name) == 0) {
            ui_utf8_glyph(out, HUB_ICONS[i].cp);
            return out;
        }
    }
    ui_utf8_glyph(out, 0xe5c3); // "apps"
    return out;
}

bool ui_media_has_art(void)
{
    return g_media.active && g_art_id != 0 && g_media.art_id == g_art_id;
}

bool ui_server_has_art(void)
{
    return g_server.now.active && g_srv_art_id != 0 && g_server.now.art_id == g_srv_art_id;
}

void ui_send_server(const char *cmd, int value)
{
    char buf[96];
    snprintf(buf, sizeof(buf), "{\"type\":\"media_server\",\"cmd\":\"%s\",\"value\":%d}", cmd, value);
    hub_server_send(buf);
}

void ui_send_media(const char *cmd, int value)
{
    char buf[96];
    snprintf(buf, sizeof(buf), "{\"type\":\"media\",\"cmd\":\"%s\",\"value\":%d}", cmd, value);
    hub_server_send(buf);
}

// ---- navigation ------------------------------------------------------------

void ui_show_page(int page)
{
    // Internet is a view inside the Stats page, not a page of its own.
    if (page == PAGE_NETWORK) {
        if (!(g_pages_mask & (1u << PAGE_NETWORK))) return;
        stats_set_view(1);
        page = PAGE_STATS;
    }
    if (page < 0 || page >= PAGE_COUNT || page == s_page) return;
    if (!(g_pages_mask & (1u << page))) page = PAGE_HOME;  // turned off in config
    // Instant switch: hiding/showing redraws only the page area once.
    for (int i = 0; i < PAGE_COUNT; i++) {
        bool on = i == page;
        if (on) lv_obj_remove_flag(s_pages[i], LV_OBJ_FLAG_HIDDEN);
        else lv_obj_add_flag(s_pages[i], LV_OBJ_FLAG_HIDDEN);
        lv_obj_set_style_bg_opa(s_nav[i], on ? LV_OPA_COVER : LV_OPA_TRANSP, 0);
        lv_obj_set_style_text_color(lv_obj_get_child(s_nav[i], 0),
                                    lv_color_hex(on ? COL_TEXT : COL_MUTED), 0);
        // The logo keeps its colours: dim it when its tab isn't selected.
        if (i == PAGE_CLAUDE) lv_obj_set_style_image_opa(lv_obj_get_child(s_nav[i], 0), on ? LV_OPA_COVER : LV_OPA_50, 0);
    }
    s_page = page;
}

static void nav_cb(lv_event_t *e)
{
    ui_show_page((int)(intptr_t)lv_event_get_user_data(e));
}

// Show only the enabled pages in the rail (flex layout closes the gaps).
static void apply_layout(void)
{
    int shown = 0;
    for (int i = 0; i < PAGE_COUNT; i++) {
        bool on = (g_pages_mask & (1u << i)) && i != PAGE_NETWORK;   // Internet lives in Stats
        if (on) lv_obj_remove_flag(s_nav[i], LV_OBJ_FLAG_HIDDEN);
        else lv_obj_add_flag(s_nav[i], LV_OBJ_FLAG_HIDDEN);
        shown += on;
    }
    // Share the rail's height between the tabs that are shown, so it is filled
    // whether 4 or 10 pages are on (capped so a few tabs don't get huge).
    int status = 2 * lv_font_get_line_height(g_icon_24) + 8;
    if (g_nav == NAV_BOTTOM) {
        // The same idea across the dock's width (the status icons take ~80 px).
        int avail = 800 - 28 - 40 - (shown + 2) * RAIL_GAP;
        int tab_w = shown ? avail / shown : 72;
        if (tab_w > 72) tab_w = 72;
        for (int i = 0; i < PAGE_COUNT; i++) lv_obj_set_size(s_nav[i], tab_w, DOCK_H - 16);
    } else {
        int avail = 480 - 2 * RAIL_PAD_V - status - (shown + 2) * RAIL_GAP;
        int tab_h = shown ? avail / shown : RAIL_TAB_MAX;
        if (tab_h > RAIL_TAB_MAX) tab_h = RAIL_TAB_MAX;
        for (int i = 0; i < PAGE_COUNT; i++) lv_obj_set_size(s_nav[i], 60, tab_h);
    }
    stats_set_net_enabled(g_pages_mask & (1u << PAGE_NETWORK));
    home_apply_layout();
    if (s_page >= 0 && !(g_pages_mask & (1u << s_page))) {
        int old = s_page;
        s_page = -1;
        lv_obj_add_flag(s_pages[old], LV_OBJ_FLAG_HIDDEN);
        ui_show_page(PAGE_HOME);
    }
}

// The page choice is kept in NVS so the board shows the right tabs at boot,
// before (or without) the companion connecting.
static void load_layout(void)
{
    nvs_handle_t h;
    uint32_t mask;
    if (nvs_open("hub", NVS_READONLY, &h) == ESP_OK) {
        if (nvs_get_u32(h, "pages", &mask) == ESP_OK) g_pages_mask = mask | 1u;
        nvs_close(h);
    }
}

// Where the navigation bar goes; read before anything is built.
static void load_nav(void)
{
    nvs_handle_t h;
    uint8_t nav;
    if (nvs_open("hub", NVS_READONLY, &h) == ESP_OK) {
        if (nvs_get_u8(h, "nav", &nav) == ESP_OK && nav <= NAV_BOTTOM) g_nav = nav;
        nvs_close(h);
    }
}

// Moving the bar changes every page's size, so save it and restart: the
// pages are rebuilt for the new space at boot.
void ui_set_nav(int nav)
{
    if (nav < NAV_LEFT || nav > NAV_BOTTOM || nav == g_nav) return;
    nvs_handle_t h;
    if (nvs_open("hub", NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_u8(h, "nav", (uint8_t)nav);
        nvs_commit(h);
        nvs_close(h);
    }
    ESP_LOGI(TAG, "navigation bar moved; restarting");
    esp_restart();
}

void ui_set_layout(uint32_t mask)
{
    mask |= 1u << PAGE_HOME;
    if (mask == g_pages_mask) return;
    g_pages_mask = mask;
    nvs_handle_t h;
    if (nvs_open("hub", NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_u32(h, "pages", mask);
        nvs_commit(h);
        nvs_close(h);
    }
    apply_layout();
}

// Tab order in the bar: page indices, Home first. Starts as the order the tabs are
// built in (Shortcuts last); the companion's [pages] list can change it.
static uint8_t s_order[PAGE_COUNT];

static void default_order(uint8_t *order)
{
    for (int n = 0; n < PAGE_COUNT; n++) {
        order[n] = n < PAGE_SHORTCUTS ? n : n == PAGE_COUNT - 1 ? PAGE_SHORTCUTS : n + 1;
    }
}

// Listed pages first (Home always leads), then the rest in the default order.
static void merge_order(const uint8_t *want, int n, uint8_t *out)
{
    bool used[PAGE_COUNT] = {0};
    int k = 0;
    out[k++] = PAGE_HOME;
    used[PAGE_HOME] = true;
    for (int i = 0; i < n; i++) {
        if (want[i] < PAGE_COUNT && !used[want[i]]) {
            out[k++] = want[i];
            used[want[i]] = true;
        }
    }
    uint8_t def[PAGE_COUNT];
    default_order(def);
    for (int i = 0; i < PAGE_COUNT; i++) {
        if (!used[def[i]]) out[k++] = def[i];
    }
}

static void apply_order(void)
{
    for (int i = 0; i < PAGE_COUNT; i++) {
        if (s_nav[s_order[i]]) lv_obj_move_to_index(s_nav[s_order[i]], i);
    }
}

static void load_order(void)
{
    default_order(s_order);
    nvs_handle_t h;
    if (nvs_open("hub", NVS_READONLY, &h) == ESP_OK) {
        uint8_t saved[PAGE_COUNT + 8];
        size_t len = sizeof(saved);
        // Saved by older firmware with fewer pages: the new ones fall in at their default spot.
        if (nvs_get_blob(h, "order", saved, &len) == ESP_OK) merge_order(saved, (int)len, s_order);
        nvs_close(h);
    }
    apply_order();
}

void ui_set_page_order(const uint8_t *order, int n)
{
    uint8_t merged[PAGE_COUNT];
    merge_order(order, n, merged);
    if (memcmp(merged, s_order, sizeof(s_order)) == 0) return;
    memcpy(s_order, merged, sizeof(s_order));
    nvs_handle_t h;
    if (nvs_open("hub", NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_blob(h, "order", order, n);
        nvs_commit(h);
        nvs_close(h);
    }
    apply_order();
}

void ui_set_home(const char *spec)
{
    home_set_layout(spec);
}

void ui_set_printers(const hub_printer_t *p, int n)
{
    memcpy(g_printers, p, sizeof(hub_printer_t) * n);
    g_printer_count = n;
    printer_refresh();
    home_refresh_printer();
}

static const char *const s_glyphs[PAGE_COUNT] = {
    ICON_HOME, ICON_INSIGHTS, ICON_AUTO_AWESOME, ICON_EVENT, ICON_WB_SUNNY, ICON_MUSIC_NOTE,
    ICON_VIEW_IN_AR, ICON_GRID_VIEW, ICON_TIMER, ICON_SPORTS_ESPORTS, ICON_PUBLIC, ICON_COMMIT,
};

void ui_rail_set_text(int page, const char *text)
{
    if (page == PAGE_CLAUDE || page < 0 || page >= PAGE_COUNT || !s_nav[page]) return;
    lv_obj_t *l = lv_obj_get_child(s_nav[page], 0);
    const lv_font_t *font = text ? g_font_16 : g_icon_32;
    if (!text) text = s_glyphs[page];
    if (lv_obj_get_style_text_font(l, 0) != font) lv_obj_set_style_text_font(l, font, 0);
    if (strcmp(lv_label_get_text(l), text) != 0) lv_label_set_text(l, text);
}

void ui_rail_set_badge(int page, uint32_t color)
{
    static lv_obj_t *s_badge[PAGE_COUNT];
    if (page < 0 || page >= PAGE_COUNT || !s_nav[page]) return;
    lv_obj_t *d = s_badge[page];
    if (!color) {
        if (d) lv_obj_add_flag(d, LV_OBJ_FLAG_HIDDEN);
        return;
    }
    if (!d) {
        d = lv_obj_create(s_nav[page]);
        lv_obj_remove_style_all(d);
        lv_obj_set_size(d, 12, 12);
        lv_obj_set_style_radius(d, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_opa(d, LV_OPA_COVER, 0);
        lv_obj_set_style_border_color(d, lv_color_hex(COL_RAIL), 0);
        lv_obj_set_style_border_width(d, 2, 0);
        lv_obj_add_flag(d, LV_OBJ_FLAG_IGNORE_LAYOUT);
        lv_obj_remove_flag(d, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_align(d, LV_ALIGN_TOP_RIGHT, -6, 3);
        s_badge[page] = d;
    }
    lv_obj_set_style_bg_color(d, lv_color_hex(color), 0);
    lv_obj_remove_flag(d, LV_OBJ_FLAG_HIDDEN);
}

static void build_rail(lv_obj_t *scr)
{
    lv_obj_t *rail = lv_obj_create(scr);
    lv_obj_remove_style_all(rail);
    lv_obj_set_style_bg_color(rail, lv_color_hex(COL_RAIL), 0);
    lv_obj_set_style_bg_opa(rail, LV_OPA_COVER, 0);
    lv_obj_set_style_border_color(rail, lv_color_hex(COL_BORDER), 0);
    lv_obj_set_style_border_width(rail, 1, 0);
    lv_obj_remove_flag(rail, LV_OBJ_FLAG_SCROLLABLE);
    if (g_nav == NAV_BOTTOM) {
        // A dock along the bottom: tabs left to right, status icons at the end.
        lv_obj_set_size(rail, 800, DOCK_H);
        lv_obj_set_pos(rail, 0, 480 - DOCK_H);
        lv_obj_set_style_border_side(rail, LV_BORDER_SIDE_TOP, 0);
        lv_obj_set_style_pad_hor(rail, 14, 0);
        lv_obj_set_flex_flow(rail, LV_FLEX_FLOW_ROW);
        lv_obj_set_style_pad_column(rail, RAIL_GAP, 0);
    } else {
        lv_obj_set_size(rail, RAIL_W, 480);
        lv_obj_set_pos(rail, g_nav == NAV_RIGHT ? 800 - RAIL_W : 0, 0);
        lv_obj_set_style_border_side(rail, g_nav == NAV_RIGHT ? LV_BORDER_SIDE_LEFT : LV_BORDER_SIDE_RIGHT, 0);
        lv_obj_set_style_pad_ver(rail, RAIL_PAD_V, 0);
        lv_obj_set_flex_flow(rail, LV_FLEX_FLOW_COLUMN);
        lv_obj_set_style_pad_row(rail, RAIL_GAP, 0);
    }
    lv_obj_set_flex_align(rail, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);

    // Tabs top to bottom: every page in PAGE_* order, except Shortcuts, which
    // always sits last, just above the status icons.
    for (int n = 0; n < PAGE_COUNT; n++) {
        int i = n < PAGE_SHORTCUTS ? n : n == PAGE_COUNT - 1 ? PAGE_SHORTCUTS : n + 1;
        lv_obj_t *b = lv_button_create(rail);
        lv_obj_remove_style_all(b);
        lv_obj_set_size(b, 60, RAIL_TAB_MAX);  // height set by apply_layout()
        lv_obj_set_style_radius(b, 14, 0);
        // Selected page: a flat raised pill behind a white icon.
        lv_obj_set_style_bg_color(b, lv_color_hex(COL_CARD_HI), 0);
        lv_obj_set_style_bg_opa(b, LV_OPA_TRANSP, 0);
        lv_obj_set_style_bg_color(b, lv_color_hex(COL_PRESSED), LV_STATE_PRESSED);
        lv_obj_set_style_bg_opa(b, LV_OPA_COVER, LV_STATE_PRESSED);
        lv_obj_t *l;
        if (i == PAGE_CLAUDE) {
            // The AI tab shows the active agent's own logo instead of a glyph.
            l = lv_image_create(b);
            lv_image_set_src(l, ui_agent_logo(32));
            s_nav_logo = l;
        } else {
            l = ui_label(b, g_icon_32, COL_MUTED, s_glyphs[i]);
        }
        lv_obj_center(l);
        lv_obj_add_event_cb(b, nav_cb, LV_EVENT_CLICKED, (void *)(intptr_t)i);
        s_nav[i] = b;
    }

    // Connection status at the bottom of the rail.
    lv_obj_t *spacer = lv_obj_create(rail);
    lv_obj_remove_style_all(spacer);
    lv_obj_set_flex_grow(spacer, 1);
    lv_obj_set_size(spacer, 1, 1);
    lv_obj_t *status = rail;
    const lv_font_t *font = g_icon_24;
    if (g_nav == NAV_BOTTOM) {
        // In the dock: PC above Wi-Fi in a small column at the end, a size smaller.
        status = lv_obj_create(rail);
        lv_obj_remove_style_all(status);
        lv_obj_set_size(status, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
        lv_obj_set_flex_flow(status, LV_FLEX_FLOW_COLUMN);
        lv_obj_set_flex_align(status, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
        lv_obj_set_style_pad_row(status, 2, 0);
        lv_obj_set_style_pad_right(status, 4, 0);
        lv_obj_remove_flag(status, LV_OBJ_FLAG_SCROLLABLE);
        font = g_icon_20;
    }
    s_pc_icon = ui_label(status, font, COL_DIM, ICON_DESKTOP_WINDOWS);
    s_wifi_icon = ui_label(status, font, COL_DIM, ICON_WIFI_OFF);
}

static void tick_cb(lv_timer_t *t)
{
    static int seconds;
    bool wifi = net_connected();
    lv_label_set_text(s_wifi_icon, wifi ? ICON_WIFI : ICON_WIFI_OFF);
    lv_obj_set_style_text_color(s_wifi_icon, lv_color_hex(wifi ? COL_GOOD : COL_BAD), 0);

    bool pc = hub_server_pc_online();
    if (pc != g_pc_online) {
        g_pc_online = pc;
        shortcuts_set_online(pc);
        if (!pc) g_media.active = false;
        // Pages show "PC offline" hints, so redraw them on every change.
        media_refresh();
        home_refresh_media();
        home_refresh_stats();
        stats_refresh();
        dash_refresh();
        claude_refresh();
        calendar_refresh(true);
        games_refresh();
        home_refresh_github();
        github_page_refresh();
    }
    lv_obj_set_style_text_color(s_pc_icon, lv_color_hex(pc ? COL_GOOD : COL_DIM), 0);

    home_tick();
    media_tick();
    claude_tick();
    games_tick();
    home_refresh_claude();
    if (++seconds % 60 == 0) {
        // Drop events that have ended.
        home_refresh_events();
        calendar_refresh(false);
        ui_calendar_badge();   // goes away once the day's last event ends
    }
}

void ui_calendar_badge(void)
{
    uint32_t color = 0;
    time_t now = time(NULL);
    struct tm tm;
    localtime_r(&now, &tm);
    if (tm.tm_year >= 120 && !home_shows_events() && (g_pages_mask & (1u << PAGE_CALENDAR))) {
        tm.tm_hour = 23; tm.tm_min = 59; tm.tm_sec = 59;
        time_t day_end = mktime(&tm);
        // Events are sorted by start: the first one not over yet is the one the
        // Calendar page lists first.
        for (int i = 0; i < g_event_count; i++) {
            const hub_event_t *e = &g_events[i];
            if (e->end <= now) continue;
            if (e->start <= day_end) color = e->color ? e->color : COL_ACCENT;
            break;
        }
    }
    ui_rail_set_badge(PAGE_CALENDAR, color);
}

// ---- public API ------------------------------------------------------------

static void page_none(lv_obj_t *page)
{
    (void)page;   // the Internet view is built by stats_create()
}

void ui_init(void)
{
    g_font_14 = inter(14, false, &lv_font_montserrat_14);
    g_font_16 = inter(16, false, &lv_font_montserrat_16);
    g_font_20 = inter(20, false, &lv_font_montserrat_20);
    g_font_24 = inter(24, false, &lv_font_montserrat_24);
    g_font_28 = inter(28, true, &lv_font_montserrat_32);
    g_font_32 = inter(32, true, &lv_font_montserrat_32);
    g_font_48 = inter(48, true, &lv_font_montserrat_48);
    size_t icon_len = icons_ttf_end - icons_ttf_start;
    g_icon_20 = lv_tiny_ttf_create_data(icons_ttf_start, icon_len, 20);
    g_icon_24 = lv_tiny_ttf_create_data(icons_ttf_start, icon_len, 24);
    g_icon_32 = lv_tiny_ttf_create_data(icons_ttf_start, icon_len, 32);
    g_icon_48 = lv_tiny_ttf_create_data(icons_ttf_start, icon_len, 48);
    g_icon_80 = lv_tiny_ttf_create_data(icons_ttf_start, icon_len, 80);
    g_font_clock = lv_tiny_ttf_create_data(clock_ttf_start, clock_ttf_end - clock_ttf_start, 128);
    // Fall back to built-in fonts so a failed TTF load can never hand LVGL a NULL font.
    // Icons then show as boxes, but the UI keeps working.
    if (!g_icon_20) g_icon_20 = (lv_font_t *)g_font_20;
    if (!g_icon_24) g_icon_24 = (lv_font_t *)g_font_24;
    if (!g_icon_32) g_icon_32 = (lv_font_t *)g_font_32;
    if (!g_icon_48) g_icon_48 = (lv_font_t *)g_font_48;
    if (!g_icon_80) g_icon_80 = (lv_font_t *)g_font_48;
    if (!g_font_clock) {
        ESP_LOGE(TAG, "clock font failed, using built-in font");
        g_font_clock = (lv_font_t *)g_font_48;
    }

    s_art_buf = heap_caps_malloc(320 * 320 * 2, MALLOC_CAP_SPIRAM);
    s_srv_art_buf = heap_caps_malloc(320 * 320 * 2, MALLOC_CAP_SPIRAM);

    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_hex(COL_BG), 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(scr, lv_color_hex(COL_TEXT), 0);
    lv_obj_remove_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

    load_nav();
    build_rail(scr);

    void (*creators[PAGE_COUNT])(lv_obj_t *) = {
        home_create, stats_create, claude_create, calendar_create, weather_page_create, media_create,
        printer_create, shortcuts_create, timer_page_create, games_page_create, page_none,
        github_page_create,
    };
    for (int i = 0; i < PAGE_COUNT; i++) {
        lv_obj_t *p = lv_obj_create(scr);
        lv_obj_remove_style_all(p);
        lv_obj_set_size(p, PAGE_W, PAGE_H);
        lv_obj_set_pos(p, g_nav == NAV_LEFT ? RAIL_W : 0, 0);
        lv_obj_set_style_pad_all(p, PAD, 0);
        lv_obj_remove_flag(p, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(p, LV_OBJ_FLAG_HIDDEN);
        s_pages[i] = p;
        creators[i](p);
    }
    dash_create(scr);
    load_layout();
    load_order();
    apply_layout();
    ui_show_page(PAGE_HOME);
    lv_timer_create(tick_cb, 1000, NULL);
    tick_cb(NULL);
}

void ui_set_stats(const hub_stats_t *s)
{
    g_stats = *s;
    stats_refresh();
    dash_mark_data();
    dash_refresh();
    home_refresh_stats();
    net_refresh_live();
}

void ui_set_media(const hub_media_t *m)
{
    bool art_changed = m->art_id != g_media.art_id || m->active != g_media.active;
    g_media = *m;
    media_refresh();
    home_refresh_media();
    if (art_changed) media_refresh_art();
}

void ui_set_art(uint32_t id, uint16_t w, uint16_t h, const uint8_t *rgb565)
{
    if (!s_art_buf) return;
    memcpy(s_art_buf, rgb565, (size_t)w * h * 2);
    lv_image_cache_drop(&g_art);
    memset(&g_art, 0, sizeof(g_art));
    g_art.header.magic = LV_IMAGE_HEADER_MAGIC;
    g_art.header.cf = LV_COLOR_FORMAT_RGB565;
    g_art.header.w = w;
    g_art.header.h = h;
    g_art.header.stride = w * 2;
    g_art.data_size = (uint32_t)w * h * 2;
    g_art.data = s_art_buf;
    g_art_id = id;
    media_refresh_art();
}

void ui_set_server(const hub_server_t *s)
{
    bool art_changed = s->now.art_id != g_server.now.art_id || s->now.active != g_server.now.active;
    g_server = *s;
    media_refresh_server();
    home_refresh_media();
    if (art_changed) media_refresh_server_art();
}

void ui_set_server_art(uint32_t id, uint16_t w, uint16_t h, const uint8_t *rgb565)
{
    if (!s_srv_art_buf) return;
    memcpy(s_srv_art_buf, rgb565, (size_t)w * h * 2);
    lv_image_cache_drop(&g_srv_art);
    memset(&g_srv_art, 0, sizeof(g_srv_art));
    g_srv_art.header.magic = LV_IMAGE_HEADER_MAGIC;
    g_srv_art.header.cf = LV_COLOR_FORMAT_RGB565;
    g_srv_art.header.w = w;
    g_srv_art.header.h = h;
    g_srv_art.header.stride = w * 2;
    g_srv_art.data_size = (uint32_t)w * h * 2;
    g_srv_art.data = s_srv_art_buf;
    g_srv_art_id = id;
    media_refresh_server_art();
}

static int event_cmp(const void *a, const void *b)
{
    const hub_event_t *x = a, *y = b;
    if (x->start != y->start) return x->start < y->start ? -1 : 1;
    return (int)y->all_day - (int)x->all_day; // all-day first on the same start
}

void ui_set_events(const hub_event_t *ev, int n, int days)
{
    memcpy(g_events, ev, sizeof(hub_event_t) * n);
    g_event_count = n;
    g_event_days = days > 0 ? days : 7;
    // The companion sends them sorted; sort anyway so day grouping can't break.
    qsort(g_events, n, sizeof(hub_event_t), event_cmp);
    calendar_refresh(true);
    home_refresh_events();
    ui_calendar_badge();
}

void ui_set_tiles(const hub_tile_t *tiles, int n)
{
    shortcuts_refresh(tiles, n);
}

void ui_set_tile_state(uint32_t on_mask)
{
    shortcuts_set_lit(on_mask);
}

void ui_set_claude(const hub_claude_t *c)
{
    bool agent_changed = strcmp(g_claude.provider, c->provider) != 0;
    g_claude = *c;
    if (agent_changed && s_nav_logo) lv_image_set_src(s_nav_logo, ui_agent_logo(32));
    claude_refresh();
    home_refresh_claude();
    dash_refresh();
}

void ui_set_weather(const hub_weather_t *w)
{
    g_weather = *w;
    weather_page_refresh();
    home_refresh_weather();
}

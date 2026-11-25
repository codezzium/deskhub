#include <stdio.h>
#include <string.h>
#include <time.h>

#include "hub_server.h"
#include "net.h"
#include "ui_internal.h"

extern const uint8_t tnum_ttf_start[] asm("_binary_Inter_SemiBold_tnum_ttf_start");
extern const uint8_t tnum_ttf_end[] asm("_binary_Inter_SemiBold_tnum_ttf_end");
extern const uint8_t ext_ttf_start[] asm("_binary_Inter_SemiBold_latin_ext_ttf_start");
extern const uint8_t ext_ttf_end[] asm("_binary_Inter_SemiBold_latin_ext_ttf_end");

#define SCR_W 800
#define SCR_H 480
#define EDGE 12
#define GAP 8
#define HEAD_H 56
#define ROW_H ((SCR_H - 2 * EDGE - HEAD_H - 4 * GAP) / 4)
#define FULL_W (SCR_W - 2 * EDGE)
#define HALF_W ((FULL_W - GAP) / 2)
#define CLAUDE_COLS 3

enum { T_CPU, T_RAM, T_GPU, T_GPU2, T_COUNT };

typedef struct {
    lv_obj_t *name, *sub, *value, *temp;
} tile_t;

static lv_obj_t *s_root;
static lv_font_t *s_big, *s_mid, *s_time, *s_date, *s_small;
static lv_obj_t *s_clock, *s_day;
static tile_t s_tiles[T_COUNT];
static lv_obj_t *s_disk_sub, *s_disk_read, *s_disk_write, *s_disk_temp;
static lv_obj_t *s_cl_name[CLAUDE_COLS], *s_cl_left[CLAUDE_COLS], *s_cl_pct[CLAUDE_COLS];
#define DEBUG_LINES 7
static lv_obj_t *s_debug, *s_dbg[DEBUG_LINES];

static const char *const MONTHS[12] = {"Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
                                       "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"};
static const char *const DAYS[7] = {"Pazar", "Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi"};

static lv_font_t *font(int px, const lv_font_t *fallback)
{
    lv_font_t *f = lv_tiny_ttf_create_data_ex(tnum_ttf_start, tnum_ttf_end - tnum_ttf_start, px,
                                              LV_FONT_KERNING_NONE, 64);
    if (!f) return (lv_font_t *)fallback;
    lv_font_t *ext = lv_tiny_ttf_create_data_ex(ext_ttf_start, ext_ttf_end - ext_ttf_start, px,
                                                LV_FONT_KERNING_NONE, 16);
    if (ext) {
        ext->fallback = fallback;
        f->fallback = ext;
    } else {
        f->fallback = fallback;
    }
    return f;
}

static lv_obj_t *card(int x, int y, int w)
{
    lv_obj_t *c = lv_obj_create(s_root);
    lv_obj_remove_style_all(c);
    lv_obj_set_pos(c, x, y);
    lv_obj_set_size(c, w, ROW_H);
    lv_obj_set_style_bg_color(c, lv_color_hex(COL_CARD), 0);
    lv_obj_set_style_bg_opa(c, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(c, 14, 0);
    lv_obj_set_style_pad_hor(c, 16, 0);
    lv_obj_set_style_pad_ver(c, 4, 0);
    lv_obj_remove_flag(c, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_remove_flag(c, LV_OBJ_FLAG_CLICKABLE);
    return c;
}

static lv_obj_t *label(lv_obj_t *parent, const lv_font_t *f, uint32_t color, lv_align_t align, int x, int y)
{
    lv_obj_t *l = ui_label(parent, f, color, "");
    lv_obj_align(l, align, x, y);
    return l;
}

static void fix_width(lv_obj_t *l, const char *sample)
{
    lv_point_t sz;
    lv_text_get_size(&sz, sample, lv_obj_get_style_text_font(l, LV_PART_MAIN), 0, 0, LV_COORD_MAX,
                     LV_TEXT_FLAG_NONE);
    lv_label_set_long_mode(l, LV_LABEL_LONG_CLIP);
    lv_obj_set_width(l, sz.x);
    lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_RIGHT, 0);
}

static lv_obj_t *rate_group(lv_obj_t *parent, int x, const char *tag)
{
    lv_obj_t *t = label(parent, s_mid, COL_MUTED, LV_ALIGN_BOTTOM_LEFT, x, 2);
    lv_label_set_text(t, tag);
    lv_obj_t *num = label(parent, s_mid, COL_TEXT, LV_ALIGN_BOTTOM_LEFT, 0, 2);
    fix_width(num, "000.00");
    lv_obj_update_layout(t);
    lv_obj_align_to(num, t, LV_ALIGN_OUT_RIGHT_BOTTOM, 4, 0);
    lv_obj_t *unit = label(parent, s_small, COL_MUTED, LV_ALIGN_BOTTOM_LEFT, 0, 0);
    lv_label_set_text(unit, "MB/s");
    lv_obj_update_layout(num);
    lv_obj_align_to(unit, num, LV_ALIGN_OUT_RIGHT_BOTTOM, 8, -6);
    return num;
}

static void make_tile(int i, int x, int y, const char *name)
{
    lv_obj_t *c = card(x, y, HALF_W);
    tile_t *t = &s_tiles[i];
    t->name = label(c, s_small, COL_MUTED, LV_ALIGN_TOP_LEFT, 0, 0);
    lv_label_set_text(t->name, name);
    t->sub = label(c, s_small, COL_MUTED, LV_ALIGN_TOP_RIGHT, 0, 0);
    t->value = label(c, s_big, COL_TEXT, LV_ALIGN_BOTTOM_LEFT, 0, 8);
    fix_width(t->value, "100%");
    t->temp = label(c, s_big, COL_GOOD, LV_ALIGN_BOTTOM_RIGHT, 0, 8);
}

static void refresh_claude(void);

#define STALE_MS 60000
static uint32_t s_last_data;
static bool s_have_data, s_was_fresh;

static bool data_fresh(void)
{
    return s_have_data && lv_tick_elaps(s_last_data) < STALE_MS;
}

void dash_mark_data(void)
{
    s_last_data = lv_tick_get();
    s_have_data = true;
}

static void refresh_clock(void)
{
    time_t now = time(NULL);
    struct tm tm;
    localtime_r(&now, &tm);
    if (tm.tm_year < 120) {
        lv_label_set_text(s_clock, "--:--:--");
        lv_label_set_text(s_day, "");
        return;
    }
    lv_label_set_text_fmt(s_clock, "%02d:%02d:%02d", tm.tm_hour, tm.tm_min, tm.tm_sec);
    lv_label_set_text_fmt(s_day, "%02d %s %02d %d %s", tm.tm_mday, MONTHS[tm.tm_mon], tm.tm_mon + 1,
                          tm.tm_year + 1900, DAYS[tm.tm_wday]);
}

static void refresh_debug(void)
{
    int rssi, channel, reason, last, avg, loss, samples;
    uint32_t disconnects;
    uint8_t bssid[6];
    net_diag_ex(&rssi, &channel, &disconnects, &reason, bssid);
    net_ping_stats(&last, &avg, &loss, &samples);
    bool wifi = net_connected();
    if (wifi) lv_label_set_text_fmt(s_dbg[0], "Wi-Fi: bağlı   %d dBm   kanal %d", rssi, channel);
    else lv_label_set_text(s_dbg[0], "Wi-Fi: bağlı değil");
    lv_label_set_text_fmt(s_dbg[1], "Ağ cihazı: %02x:%02x:%02x:%02x:%02x:%02x%s", bssid[0], bssid[1], bssid[2],
                          bssid[3], bssid[4], bssid[5], net_pinned() ? "   (sabit)" : "");
    if (samples == 0) lv_label_set_text(s_dbg[2], "Modem ping: ölçüm yok");
    else if (last < 0) lv_label_set_text_fmt(s_dbg[2], "Modem ping: kayıp   ort %d ms", avg);
    else lv_label_set_text_fmt(s_dbg[2], "Modem ping: %d ms   ort %d ms", last, avg);
    lv_label_set_text_fmt(s_dbg[3], "Kayıp: %%%d   (son %d ölçüm)", loss, samples);
    lv_label_set_text_fmt(s_dbg[4], "Kopma: %lu   son sebep %d", (unsigned long)disconnects, reason);
    lv_label_set_text_fmt(s_dbg[5], "PC bağlantısı: %s", hub_server_link());
    lv_label_set_text_fmt(s_dbg[6], "IP %s   açık %lu dk", wifi ? net_ip() : "-",
                          (unsigned long)(lv_tick_get() / 60000));
}

static void tick_cb(lv_timer_t *t)
{
    refresh_clock();
    bool fresh = data_fresh();
    if (fresh != s_was_fresh) {
        s_was_fresh = fresh;
        dash_refresh();
    } else {
        refresh_claude();
    }
    if (!lv_obj_has_flag(s_debug, LV_OBJ_FLAG_HIDDEN)) refresh_debug();
}

static void toggle_debug(lv_event_t *e)
{
    if (lv_obj_has_flag(s_debug, LV_OBJ_FLAG_HIDDEN)) {
        refresh_debug();
        lv_obj_remove_flag(s_debug, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(s_debug, LV_OBJ_FLAG_HIDDEN);
    }
}

static void make_debug(void)
{
    s_debug = lv_obj_create(s_root);
    lv_obj_remove_style_all(s_debug);
    lv_obj_set_size(s_debug, SCR_W, SCR_H);
    lv_obj_set_style_bg_color(s_debug, lv_color_hex(COL_BG), 0);
    lv_obj_set_style_bg_opa(s_debug, LV_OPA_COVER, 0);
    lv_obj_remove_flag(s_debug, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_remove_flag(s_debug, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(s_debug, LV_OBJ_FLAG_HIDDEN);
    lv_obj_t *title = ui_label(s_debug, s_small, COL_MUTED, "TEŞHİS   (geri dönmek için dokun)");
    lv_obj_set_pos(title, EDGE + 4, EDGE);
    for (int i = 0; i < DEBUG_LINES; i++) {
        s_dbg[i] = ui_label(s_debug, s_date, i == 2 || i == 3 ? COL_ACCENT : COL_TEXT, "");
        lv_obj_set_pos(s_dbg[i], EDGE + 4, EDGE + 44 + i * 58);
    }
}

void dash_create(lv_obj_t *scr)
{
    s_big = font(56, g_font_48);
    s_mid = font(38, g_font_32);
    s_time = font(48, g_font_48);
    s_date = font(26, g_font_24);
    s_small = font(20, g_font_20);

    s_root = lv_obj_create(scr);
    lv_obj_remove_style_all(s_root);
    lv_obj_set_pos(s_root, 0, 0);
    lv_obj_set_size(s_root, SCR_W, SCR_H);
    lv_obj_set_style_bg_color(s_root, lv_color_hex(COL_BG), 0);
    lv_obj_set_style_bg_opa(s_root, LV_OPA_COVER, 0);
    lv_obj_remove_flag(s_root, LV_OBJ_FLAG_SCROLLABLE);

    s_clock = ui_label(s_root, s_time, COL_TEXT, "");
    lv_obj_set_pos(s_clock, EDGE + 4, EDGE - 2);
    s_day = ui_label(s_root, s_date, COL_MUTED, "");
    lv_obj_align(s_day, LV_ALIGN_TOP_RIGHT, -EDGE - 4, EDGE + 14);

    int y = EDGE + HEAD_H + GAP;
    make_tile(T_CPU, EDGE, y, "CPU");
    make_tile(T_RAM, EDGE + HALF_W + GAP, y, "RAM");
    y += ROW_H + GAP;
    make_tile(T_GPU, EDGE, y, "GPU");
    make_tile(T_GPU2, EDGE + HALF_W + GAP, y, "GPU 2");
    y += ROW_H + GAP;

    lv_obj_t *d = card(EDGE, y, FULL_W);
    lv_obj_t *dn = label(d, s_small, COL_MUTED, LV_ALIGN_TOP_LEFT, 0, 0);
    lv_label_set_text(dn, "DİSK");
    s_disk_sub = label(d, s_small, COL_MUTED, LV_ALIGN_TOP_RIGHT, 0, 0);
    s_disk_read = rate_group(d, 0, "R");
    s_disk_write = rate_group(d, 300, "W");
    s_disk_temp = label(d, s_big, COL_GOOD, LV_ALIGN_BOTTOM_RIGHT, 0, 8);
    y += ROW_H + GAP;

    lv_obj_t *c = card(EDGE, y, FULL_W);
    static const int col_x[CLAUDE_COLS] = {0, 200, 470};
    for (int i = 0; i < CLAUDE_COLS; i++) {
        int x = col_x[i];
        s_cl_name[i] = label(c, s_small, COL_MUTED, LV_ALIGN_TOP_LEFT, x, 0);
        s_cl_pct[i] = label(c, s_big, COL_TEXT, LV_ALIGN_BOTTOM_LEFT, x, 8);
        fix_width(s_cl_pct[i], "100%");
        s_cl_left[i] = ui_label(c, s_small, COL_TEXT, "");
    }

    make_debug();
    lv_obj_add_event_cb(s_root, toggle_debug, LV_EVENT_CLICKED, NULL);
    lv_timer_create(tick_cb, 1000, NULL);
    refresh_clock();
    dash_refresh();
}

static uint32_t level_color(float v, float warn, float bad)
{
    return v >= bad ? COL_BAD : v >= warn ? COL_WARN : COL_GOOD;
}

static void set_temp(lv_obj_t *l, float t, float warn, float bad)
{
    if (t < 0) {
        lv_label_set_text(l, "");
        return;
    }
    lv_label_set_text_fmt(l, "%d\xC2\xB0" "C", (int)(t + 0.5f));
    lv_obj_set_style_text_color(l, lv_color_hex(level_color(t, warn, bad)), 0);
}

static void set_pct(lv_obj_t *l, float pct)
{
    lv_label_set_text_fmt(l, "%d%%", (int)(pct + 0.5f));
}

static const char *short_name(const char *name, const char *fallback)
{
    if (!name[0]) return fallback;
    if (strncmp(name, "GeForce ", 8) == 0) name += 8;
    if (strncmp(name, "RTX ", 4) == 0) name += 4;
    return name;
}

static void set_gpu(tile_t *t, bool has, float load, float temp, float used, float total, const char *name,
                    const char *fallback)
{
    if (!has) {
        lv_label_set_text(t->name, fallback);
        lv_label_set_text(t->value, "--");
        lv_label_set_text(t->sub, "");
        lv_label_set_text(t->temp, "");
        return;
    }
    lv_label_set_text_fmt(t->name, "%.20s", short_name(name, fallback));
    set_pct(t->value, load);
    if (total > 0) lv_label_set_text_fmt(t->sub, "%.1f / %.0f GB", used, total);
    else lv_label_set_text(t->sub, "");
    set_temp(t->temp, temp, 75, 85);
}

static void set_rate(lv_obj_t *l, float kbps)
{
    float mb = kbps / 1024;
    if (mb >= 1000) lv_label_set_text_fmt(l, "%.0f", mb);
    else if (mb >= 100) lv_label_set_text_fmt(l, "%.1f", mb);
    else lv_label_set_text_fmt(l, "%.2f", mb);
}

static void fmt_left(char *buf, size_t n, time_t reset)
{
    time_t now = time(NULL);
    struct tm tm;
    localtime_r(&now, &tm);
    if (!reset || tm.tm_year < 120) {
        buf[0] = 0;
        return;
    }
    long s = (long)(reset - now);
    if (s < 0) s = 0;
    long d = s / 86400, h = (s % 86400) / 3600, m = (s % 3600) / 60;
    if (d > 0) snprintf(buf, n, "%ld gün %ld saat", d, h);
    else if (h > 0) snprintf(buf, n, "%ld saat %ld dk", h, m);
    else snprintf(buf, n, "%ld dk", m);
}

static void set_claude_col(int i, const char *name, bool on, float pct, time_t reset)
{
    char left[32];
    lv_label_set_text(s_cl_name[i], name);
    if (!on) {
        lv_label_set_text(s_cl_pct[i], name[0] ? "--" : "");
        lv_obj_set_style_text_color(s_cl_pct[i], lv_color_hex(COL_DIM), 0);
        lv_label_set_text(s_cl_left[i], "");
        return;
    }
    set_pct(s_cl_pct[i], pct);
    lv_obj_set_style_text_color(s_cl_pct[i], lv_color_hex(level_color(pct, 50, 80)), 0);
    fmt_left(left, sizeof(left), reset);
    lv_label_set_text(s_cl_left[i], left);
    lv_obj_update_layout(s_cl_name[i]);
    lv_obj_align_to(s_cl_left[i], s_cl_name[i], LV_ALIGN_OUT_RIGHT_MID, 10, 0);
}

static void refresh_claude(void)
{
    if (!s_root) return;
    const hub_claude_t *c = &g_claude;
    bool live = data_fresh() && c->valid;
    set_claude_col(0, "5 SAAT", live && c->has_5h, c->pct_5h, c->reset_5h);
    for (int i = 1; i < CLAUDE_COLS; i++) {
        int w = i - 1;
        bool on = live && w < c->weekly_count;
        const char *name = w < c->weekly_count ? c->weekly_name[w] : (i == 1 ? "HAFTALIK" : "");
        set_claude_col(i, name, on, on ? c->weekly_pct[w] : 0, on ? c->weekly_reset[w] : 0);
    }
}

void dash_refresh(void)
{
    if (!s_root) return;
    refresh_claude();
    const hub_stats_t *s = &g_stats;
    if (!data_fresh()) {
        for (int i = 0; i < T_COUNT; i++) {
            lv_label_set_text(s_tiles[i].value, "--");
            lv_label_set_text(s_tiles[i].temp, "");
            lv_label_set_text(s_tiles[i].sub, i == T_CPU ? "PC bağlı değil" : "");
        }
        lv_label_set_text(s_disk_read, "--");
        lv_label_set_text(s_disk_write, "--");
        lv_label_set_text(s_disk_temp, "");
        lv_label_set_text(s_disk_sub, "");
        return;
    }

    set_pct(s_tiles[T_CPU].value, s->cpu);
    lv_label_set_text(s_tiles[T_CPU].sub, "");
    set_temp(s_tiles[T_CPU].temp, s->cpu_temp, 75, 88);

    if (s->ram_total > 0) {
        set_pct(s_tiles[T_RAM].value, 100.0f * s->ram_used / s->ram_total);
        lv_label_set_text_fmt(s_tiles[T_RAM].sub, "%.1f / %.0f GB", s->ram_used, s->ram_total);
    }
    lv_label_set_text(s_tiles[T_RAM].temp, "");

    set_gpu(&s_tiles[T_GPU], s->has_gpu, s->gpu, s->gpu_temp, s->vram_used, s->vram_total, s->gpu_name, "GPU");
    set_gpu(&s_tiles[T_GPU2], s->has_gpu2, s->gpu2, s->gpu2_temp, s->vram2_used, s->vram2_total, s->gpu2_name,
            "GPU 2");

    set_rate(s_disk_read, s->disk_read);
    set_rate(s_disk_write, s->disk_write);
    set_temp(s_disk_temp, s->disk_temp, 65, 75);
    lv_label_set_text_fmt(s_disk_sub, "%d%% dolu", (int)(s->disk + 0.5f));
}

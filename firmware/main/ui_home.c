// Home: widgets placed on a 5 x 3 grid from {"type":"home","grid":[...]}.
//
// Older companions send "left"/"right" columns of widget names instead (see
// home_apply_layout); there each widget has a natural height and "events"
// stretches to fill what is left of its column. Widgets that don't fit, or whose
// page is turned off, are hidden. The layout is saved in NVS for the next boot.
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "icons.h"
#include "logos.h"
#include "net.h"
#include "nvs.h"
#include "sdkconfig.h"
#include "ui_internal.h"
#include "weather.h"

#define LEFT_W 384
#define RIGHT_X (LEFT_W + 16)
#define RIGHT_W (CONTENT_W - RIGHT_X)
#define GAP (UI_COMPACT ? 8 : 12)
#define EVENT_ROWS_MAX 3

enum { W_CLOCK, W_CLAUDE, W_EVENTS, W_WEATHER, W_MEDIA, W_STATS, W_PRINTER, W_GAME, W_NET, W_GITHUB, W_COUNT };
static const char *const W_NAMES[W_COUNT] = {"clock", "claude", "events", "weather", "media", "stats", "printer", "game", "net",
                                             "github"};
static const int W_HEIGHT[W_COUNT] = {152, 88, 176, 120, 120, 120, 120, 120, 120, 120};
// Above the bottom dock: three widgets still fit in a column under the top bar.
static const int W_HEIGHT_COMPACT[W_COUNT] = {152, 80, 176, 100, 100, 100, 100, 100, 100, 100};
static const int W_PAGE[W_COUNT] = {-1, PAGE_CLAUDE, PAGE_CALENDAR, PAGE_WEATHER, PAGE_MEDIA, PAGE_STATS, PAGE_PRINTER, PAGE_GAMES, PAGE_NETWORK,
                                    -1};
#define W_MIN_EVENTS 120

static lv_obj_t *s_w[W_COUNT];
static char s_spec[256] = "header;claude,events|weather,media,stats";

// Optional slim top bar with the time and date (instead of the clock widget).
#define HEADER_H 44
static bool s_header;
static int s_top;  // y where the columns start: 0, or below the top bar
static lv_obj_t *s_hdr, *s_hdr_time, *s_hdr_ampm, *s_hdr_date;

static lv_obj_t *s_ampm, *s_date, *s_date_dot;
static lv_obj_t *s_ev_hdr, *s_ev_rows[EVENT_ROWS_MAX], *s_ev_dot[EVENT_ROWS_MAX], *s_ev_time[EVENT_ROWS_MAX],
    *s_ev_title[EVENT_ROWS_MAX], *s_ev_empty;
static int s_ev_max = 2, s_ev_inner;
#define EV_PITCH 60   // a 56 px event row and the gap under it
static lv_obj_t *s_wx_icon, *s_wx_temp, *s_wx_text, *s_wx_range;
static lv_obj_t *s_md_art, *s_md_frame, *s_md_ph, *s_md_title, *s_md_artist, *s_md_btn;
static lv_obj_t *s_st_name[3], *s_st_bar[3], *s_st_val[3];
static lv_obj_t *s_cl_cap[2], *s_cl_val[2], *s_cl_bar[2], *s_cl_sub[2], *s_cl_badge, *s_cl_icon;
static lv_obj_t *s_cl_card;

lv_obj_t *home_claude_card(void)
{
    return s_cl_card;
}
static lv_obj_t *s_pr_name, *s_pr_state, *s_pr_file, *s_pr_bar, *s_pr_info;

// The media widget shows the PC, or the media server's session while the PC is idle.
static bool home_shows_server(void)
{
    return !g_media.active && g_server.configured && g_server.now.active;
}

static void play_cb(lv_event_t *e)
{
    if (home_shows_server()) ui_send_server("toggle", 0);
    else ui_send_media("toggle", 0);
}

static void open_page_cb(lv_event_t *e)
{
    ui_show_page((int)(intptr_t)lv_event_get_user_data(e));
}

// Height inside a widget card of outer height h (padding and border taken off).
static int inner_height(lv_obj_t *card, int h)
{
    return h - lv_obj_get_style_pad_top(card, 0) - lv_obj_get_style_pad_bottom(card, 0)
         - 2 * lv_obj_get_style_border_width(card, 0);
}

static lv_obj_t *widget_card(lv_obj_t *parent, int page)
{
    lv_obj_t *c = ui_card(parent);
    if (UI_COMPACT) lv_obj_set_style_pad_ver(c, 9, 0);
    lv_obj_add_flag(c, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_style_bg_color(c, lv_color_hex(COL_CARD_HI), LV_STATE_PRESSED);
    lv_obj_add_event_cb(c, open_page_cb, LV_EVENT_CLICKED, (void *)(intptr_t)page);
    return c;
}

// ---- widgets: create once, lay out for any width ---------------------------

// ---- clock ----------------------------------------------------------------
// Each character of the time sits in its own fixed-width cell (all digits as
// wide as "0"), like a digital clock. The clock then fills its column exactly
// and doesn't shift as the minutes change. The font is rendered at the size
// the column needs (no image scaling, which is slow and blurry).
#define CLOCK_PX 128
#define CLOCK_DATE_GAP 10
static int s_date_px = 24;   // date font size; the clock widget's height follows it
#define CLOCK_DATE_H (s_date_px * 5 / 4 + 2)
static void update_date(const struct tm *tm);
#define CLOCK_CELLS 5

static lv_obj_t *s_clock_box, *s_cell[CLOCK_CELLS];
static char s_time[8] = "--:--";
static lv_font_t *s_clock_font;
static bool s_clock_narrow;

typedef struct {
    int px;         // font size to render at
    int cell, colon;  // cell widths at that size
    int digit_top;  // line top -> top of the digits
    int digit_h;
    int height;     // widget height: digits + gap + date
    bool narrow;    // AM/PM moves to the date line
} clock_geo_t;

static int time_digits(void)
{
    int n = 0;
    for (const char *p = s_time; *p; p++) n += (*p >= '0' && *p <= '9') || *p == '-';
    return n ? n : 4;
}

// Geometry from the 128 px font's metrics, scaled linearly (no font is created here).
// max_h > 0: also shrink the digits so the clock and its date fit that height (grid cells).
static clock_geo_t clock_geo(int w, int max_h)
{
    clock_geo_t g;
    const lv_font_t *f = g_font_clock;
    int cell = lv_font_get_glyph_width(f, '0', '0');
    int colon = lv_font_get_glyph_width(f, ':', '0');
    int tw = time_digits() * cell + colon;
    g.narrow = w < 340;
    int avail = g.narrow ? w : w - 64;  // the wide column keeps room for AM/PM beside it
    g.px = tw > 0 ? CLOCK_PX * avail / tw : CLOCK_PX;
    if (!g.narrow && g.px > CLOCK_PX) g.px = CLOCK_PX;
    if (g.px > 170) g.px = 170;
    lv_font_glyph_dsc_t d;
    lv_font_get_glyph_dsc(f, &d, '0', 0);
    if (max_h > 0 && d.box_h > 0) {
        int room = max_h - CLOCK_DATE_GAP - CLOCK_DATE_H;   // left for the digits
        int fit = room * CLOCK_PX / d.box_h;
        if (fit < g.px) g.px = fit < 24 ? 24 : fit;
    }
    int ascent = f->line_height - f->base_line;
    g.cell = cell * g.px / CLOCK_PX;
    g.colon = colon * g.px / CLOCK_PX;
    g.digit_top = (ascent - (d.box_h + d.ofs_y)) * g.px / CLOCK_PX;
    g.digit_h = d.box_h * g.px / CLOCK_PX;
    g.height = g.digit_h + CLOCK_DATE_GAP + CLOCK_DATE_H;
    return g;
}

static void create_clock(lv_obj_t *page)
{
    lv_obj_t *w = lv_obj_create(page);
    lv_obj_remove_style_all(w);
    lv_obj_remove_flag(w, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_flag(w, LV_OBJ_FLAG_OVERFLOW_VISIBLE);
    s_clock_box = lv_obj_create(w);
    lv_obj_remove_style_all(s_clock_box);
    lv_obj_remove_flag(s_clock_box, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_flag(s_clock_box, LV_OBJ_FLAG_OVERFLOW_VISIBLE);
    for (int i = 0; i < CLOCK_CELLS; i++) {
        s_cell[i] = ui_label(s_clock_box, g_font_clock, COL_TEXT, "");
        lv_obj_set_style_text_align(s_cell[i], LV_TEXT_ALIGN_CENTER, 0);
    }
    s_clock_font = g_font_clock;
    s_ampm = ui_label(w, g_font_24, COL_MUTED, "");
    s_date = ui_label(w, g_font_24, COL_MUTED, "Waiting for network");
    // Separator dot between the date and AM/PM in the narrow column.
    s_date_dot = lv_obj_create(w);
    lv_obj_remove_style_all(s_date_dot);
    lv_obj_set_size(s_date_dot, 5, 5);
    lv_obj_set_style_radius(s_date_dot, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(s_date_dot, lv_color_hex(COL_MUTED), 0);
    lv_obj_set_style_bg_opa(s_date_dot, LV_OPA_70, 0);
    lv_obj_remove_flag(s_date_dot, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_flag(s_date_dot, LV_OBJ_FLAG_HIDDEN);
    lv_label_set_long_mode(s_date, LV_LABEL_LONG_DOT);
    s_w[W_CLOCK] = w;
}

static void set_clock_text(const clock_geo_t *g)
{
    int x = 0, i = 0;
    char ch[2] = {0};
    for (const char *p = s_time; *p && i < CLOCK_CELLS; p++, i++) {
        int cw = *p == ':' ? g->colon : g->cell;
        ch[0] = *p;
        lv_label_set_text(s_cell[i], ch);
        lv_obj_set_width(s_cell[i], cw);
        lv_obj_set_pos(s_cell[i], x, 0);
        lv_obj_remove_flag(s_cell[i], LV_OBJ_FLAG_HIDDEN);
        x += cw;
    }
    for (; i < CLOCK_CELLS; i++) lv_obj_add_flag(s_cell[i], LV_OBJ_FLAG_HIDDEN);
    lv_obj_set_size(s_clock_box, x, LV_SIZE_CONTENT);
    if (!g->narrow) lv_obj_set_pos(s_ampm, x + 8, g->digit_h - 28);
}

static void layout_clock(int w, int h)
{
    clock_geo_t g = clock_geo(w, h);
    s_clock_narrow = g.narrow;
    lv_font_t *old = NULL;
    lv_font_t *font = ui_clock_font(g.px, &old);
    // A failed font creation falls back to the 128 px font: shrink the cells to match.
    if (font == g_font_clock && g.px != CLOCK_PX) g.px = CLOCK_PX;
    for (int i = 0; i < CLOCK_CELLS; i++) lv_obj_set_style_text_font(s_cell[i], font, 0);
    if (old) lv_tiny_ttf_destroy(old);  // no label uses it any more
    s_clock_font = font;
    // Top of the digits at the top of the widget.
    lv_obj_set_pos(s_clock_box, 0, -g.digit_top);
    set_clock_text(&g);
    // Wide: AM/PM beside the clock. Narrow: update_date puts it at the right end of the date line.
    if (!g.narrow) lv_obj_remove_flag(s_ampm, LV_OBJ_FLAG_HIDDEN);
    lv_obj_set_pos(s_date, 0, g.digit_h + CLOCK_DATE_GAP);
    lv_obj_set_width(s_date, w);
}

static int widget_height(int id, int w)
{
    return id == W_CLOCK ? clock_geo(w, 0).height : (UI_COMPACT ? W_HEIGHT_COMPACT : W_HEIGHT)[id];
}

static void create_claude(lv_obj_t *page)
{
    lv_obj_t *c = widget_card(page, PAGE_CLAUDE);
    s_cl_card = c;
    lv_obj_set_style_pad_ver(c, UI_COMPACT ? 6 : 10, 0);
    // AI badge: the agent's logo on a tile tinted in its brand colour.
    s_cl_badge = lv_obj_create(c);
    lv_obj_remove_style_all(s_cl_badge);
    lv_obj_remove_flag(s_cl_badge, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_radius(s_cl_badge, 12, 0);
    lv_obj_set_style_bg_color(s_cl_badge, lv_color_hex(ui_agent_color()), 0);
    lv_obj_set_style_bg_opa(s_cl_badge, LV_OPA_20, 0);
    s_cl_icon = lv_image_create(s_cl_badge);
    lv_image_set_src(s_cl_icon, ui_agent_logo(24));
    lv_obj_center(s_cl_icon);
    static const char *caps[2] = {"5-HOUR LIMIT", "WEEKLY LIMIT"};
    for (int i = 0; i < 2; i++) {
        s_cl_cap[i] = ui_label(c, g_font_14, COL_MUTED, caps[i]);
        s_cl_val[i] = ui_label(c, g_font_20, COL_TEXT, "--");
        lv_obj_set_width(s_cl_val[i], 64);
        lv_obj_set_style_text_align(s_cl_val[i], LV_TEXT_ALIGN_RIGHT, 0);
        s_cl_bar[i] = ui_bar(c, COL_GOOD);
        s_cl_sub[i] = ui_label(c, g_font_14, COL_MUTED, "");
        ui_marquee(s_cl_sub[i]);
    }
    s_w[W_CLAUDE] = c;
}

static void layout_claude(int w, int h)
{
    // One grid cell wide: just the 5-hour limit, no badge.
    bool tiny = w < 200;
    for (int i = 0; i < 4; i++) {
        lv_obj_t *o[4] = {s_cl_cap[1], s_cl_val[1], s_cl_bar[1], s_cl_sub[1]};
        if (tiny) lv_obj_add_flag(o[i], LV_OBJ_FLAG_HIDDEN);
        else lv_obj_remove_flag(o[i], LV_OBJ_FLAG_HIDDEN);
    }
    if (tiny) lv_obj_add_flag(s_cl_badge, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_remove_flag(s_cl_badge, LV_OBJ_FLAG_HIDDEN);
    // Badge on the left: a 40 px tile, or just the icon in the narrow column.
    bool narrow = w < 340;
    int badge = narrow ? 24 : 40;
    lv_obj_set_size(s_cl_badge, badge, badge);
    lv_obj_set_style_bg_opa(s_cl_badge, narrow ? LV_OPA_TRANSP : LV_OPA_20, 0);
    lv_obj_align(s_cl_badge, LV_ALIGN_LEFT_MID, 0, 0);
    int x0 = tiny ? 0 : badge + (narrow ? 8 : 12);
    int colw = tiny ? w - 32 : (w - 32 - x0 - 14) / 2;
    // Fill the card's height: caption and value on the first line, the reset line
    // at the bottom, the bar centred between them.
    int inner_h = inner_height(s_w[W_CLAUDE], h);
    int val_h = lv_font_get_line_height(g_font_20), sub_h = lv_font_get_line_height(g_font_14);
    int sub_y = inner_h - sub_h;
    int bar_y = (val_h + sub_y) / 2 - 5;
    if (sub_y < val_h + 14) {   // too short for three lines: keep them packed
        bar_y = val_h + 4;
        sub_y = bar_y + 14;
    }
    for (int i = 0; i < (tiny ? 1 : 2); i++) {
        int x = x0 + i * (colw + 14);
        // Captions shrink with the column so they never run into the percentage.
        const char *cap = colw >= 175 ? (i ? "WEEKLY LIMIT" : "5-HOUR LIMIT")
                        : colw >= 110 ? (i ? "WEEKLY" : "5-HOUR") : (i ? "WEEK" : "5 HR");
        lv_label_set_text(s_cl_cap[i], cap);
        lv_obj_set_pos(s_cl_cap[i], x, 3);
        lv_obj_set_pos(s_cl_val[i], x + colw - 64, 0);
        lv_obj_set_size(s_cl_bar[i], colw, 10);
        lv_obj_set_pos(s_cl_bar[i], x, bar_y);
        lv_obj_set_width(s_cl_sub[i], colw);
        lv_obj_set_pos(s_cl_sub[i], x, sub_y);
    }
}

static void create_events(lv_obj_t *page)
{
    lv_obj_t *ev = widget_card(page, PAGE_CALENDAR);
    s_ev_hdr = ui_caption(ev, "UP NEXT");
    lv_obj_set_pos(s_ev_hdr, 0, 0);
    for (int i = 0; i < EVENT_ROWS_MAX; i++) {
        lv_obj_t *row = lv_obj_create(ev);
        lv_obj_remove_style_all(row);
        lv_obj_remove_flag(row, LV_OBJ_FLAG_CLICKABLE);
        s_ev_dot[i] = lv_obj_create(row);
        lv_obj_remove_style_all(s_ev_dot[i]);
        lv_obj_set_size(s_ev_dot[i], 4, 48);
        lv_obj_set_style_radius(s_ev_dot[i], 2, 0);
        lv_obj_set_style_bg_opa(s_ev_dot[i], LV_OPA_COVER, 0);
        lv_obj_align(s_ev_dot[i], LV_ALIGN_LEFT_MID, 0, 0);
        s_ev_title[i] = ui_label(row, g_font_20, COL_TEXT, "");
        ui_marquee(s_ev_title[i]);
        lv_obj_set_pos(s_ev_title[i], 14, 2);
        s_ev_time[i] = ui_label(row, g_font_16, COL_MUTED, "");
        lv_obj_set_pos(s_ev_time[i], 14, 30);
        s_ev_rows[i] = row;
    }
    // Under the last row when there's room: "No more events in the next 7 days".
    s_ev_empty = ui_label(ev, g_font_16, COL_MUTED, "");
    ui_marquee(s_ev_empty);
    s_w[W_EVENTS] = ev;
}

static void layout_events(int w, int h)
{
    // Header 26, then as many 56 px rows as fit with 4 px between them, stacked
    // from the top. Space left under the rows gets the "no more events" line.
    s_ev_inner = inner_height(s_w[W_EVENTS], h);
    s_ev_max = (s_ev_inner - 26 + 4) / EV_PITCH;
    if (s_ev_max < 1) s_ev_max = 1;
    if (s_ev_max > EVENT_ROWS_MAX) s_ev_max = EVENT_ROWS_MAX;
    for (int i = 0; i < EVENT_ROWS_MAX; i++) {
        lv_obj_set_size(s_ev_rows[i], w - 32, 56);
        lv_obj_set_pos(s_ev_rows[i], 0, 26 + i * EV_PITCH);
        lv_obj_set_width(s_ev_title[i], w - 52);
    }
    lv_obj_set_width(s_ev_empty, w - 32 - 14);
    home_refresh_events();
}

static void create_weather(lv_obj_t *page)
{
    lv_obj_t *wx = widget_card(page, PAGE_WEATHER);
    s_wx_icon = ui_label(wx, g_icon_80, COL_WARN, ICON_CLOUD);
    lv_obj_align(s_wx_icon, LV_ALIGN_LEFT_MID, -4, 0);
    s_wx_temp = ui_label(wx, g_font_48, COL_TEXT, "--");
    lv_obj_set_pos(s_wx_temp, 96, -8);
    s_wx_text = ui_label(wx, g_font_16, COL_MUTED, "Loading weather");
    lv_obj_set_pos(s_wx_text, 98, UI_COMPACT ? 42 : 46);
    s_wx_range = ui_label(wx, g_font_16, COL_MUTED, "");
    lv_obj_set_pos(s_wx_range, 98, UI_COMPACT ? 61 : 66);
    s_w[W_WEATHER] = wx;
}

static void create_media(lv_obj_t *page)
{
    lv_obj_t *md = widget_card(page, PAGE_MEDIA);
    s_md_ph = lv_obj_create(md);
    lv_obj_remove_style_all(s_md_ph);
    lv_obj_set_size(s_md_ph, 64, 64);
    lv_obj_set_style_radius(s_md_ph, 12, 0);
    lv_obj_set_style_bg_color(s_md_ph, lv_color_hex(COL_CARD_HI), 0);
    lv_obj_set_style_bg_opa(s_md_ph, LV_OPA_COVER, 0);
    lv_obj_remove_flag(s_md_ph, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_center(ui_label(s_md_ph, g_icon_32, COL_MUTED, ICON_MUSIC_NOTE));
    s_md_frame = lv_obj_create(md);
    lv_obj_remove_style_all(s_md_frame);
    lv_obj_set_size(s_md_frame, 64, 64);
    lv_obj_set_style_radius(s_md_frame, 12, 0);
    lv_obj_set_style_clip_corner(s_md_frame, true, 0);
    lv_obj_remove_flag(s_md_frame, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    s_md_art = lv_image_create(s_md_frame);
    lv_obj_set_size(s_md_art, 64, 64);
    lv_image_set_inner_align(s_md_art, LV_IMAGE_ALIGN_STRETCH);
    lv_obj_add_flag(s_md_art, LV_OBJ_FLAG_HIDDEN);
    s_md_title = ui_label(md, g_font_20, COL_TEXT, "Nothing playing");
    ui_marquee(s_md_title);
    lv_obj_set_pos(s_md_title, 76, 2);
    s_md_artist = ui_label(md, g_font_16, COL_MUTED, "");
    ui_marquee(s_md_artist);
    lv_obj_set_pos(s_md_artist, 76, 30);
    s_md_btn = ui_round_button(md, 40, COL_CARD_HI, g_icon_24, ICON_PLAY_ARROW, play_cb, NULL);
    lv_obj_align(s_md_btn, LV_ALIGN_BOTTOM_RIGHT, 0, 0);
    s_w[W_MEDIA] = md;
}

static void layout_media(int w, int h)
{
    // Cover, title and artist centred in the card's height.
    int inner = inner_height(s_w[W_MEDIA], h);
    int art_y = inner > 64 ? (inner - 64) / 2 : 0;
    int text_y = inner > 50 ? (inner - 50) / 2 : 0;
    lv_obj_set_pos(s_md_ph, 0, art_y);
    lv_obj_set_pos(s_md_frame, 0, art_y);
    lv_obj_set_pos(s_md_title, 76, text_y + 2);
    lv_obj_set_pos(s_md_artist, 76, text_y + 30);
    lv_obj_set_width(s_md_title, w - 32 - 76);
    lv_obj_set_width(s_md_artist, w - 32 - 76);
}

static void create_stats(lv_obj_t *page)
{
    lv_obj_t *st = widget_card(page, PAGE_STATS);
    static const char *names[3] = {"CPU", "GPU", "RAM"};
    static const uint32_t colors[3] = {COL_ACCENT2, COL_CYAN, COL_DATA3};
    for (int i = 0; i < 3; i++) {
        s_st_name[i] = ui_label(st, g_font_16, COL_MUTED, names[i]);
        lv_obj_set_pos(s_st_name[i], 0, i * 30);
        s_st_bar[i] = ui_bar(st, colors[i]);
        s_st_val[i] = ui_label(st, g_font_16, COL_TEXT, "--");
        lv_obj_set_width(s_st_val[i], 48);
        lv_obj_set_style_text_align(s_st_val[i], LV_TEXT_ALIGN_RIGHT, 0);
    }
    s_w[W_STATS] = st;
}

static void layout_stats(int w, int h)
{
    // Rows fill the card's height; one cell wide drops the bars (names and values only).
    int inner = inner_height(s_w[W_STATS], h);
    int step = (inner - lv_font_get_line_height(g_font_16)) / 2;
    bool bars = w >= 200;
    for (int i = 0; i < 3; i++) {
        lv_obj_set_pos(s_st_name[i], 0, i * step);
        if (bars) lv_obj_remove_flag(s_st_bar[i], LV_OBJ_FLAG_HIDDEN);
        else lv_obj_add_flag(s_st_bar[i], LV_OBJ_FLAG_HIDDEN);
        lv_obj_set_width(s_st_bar[i], w - 32 - 48 - 56);
        lv_obj_set_pos(s_st_bar[i], 48, i * step + 6);
        lv_obj_set_pos(s_st_val[i], w - 32 - 48, i * step);
    }
}

static void layout_weather(int w, int h)
{
    // One cell wide: a smaller icon beside the temperature, nothing else.
    bool tiny = w < 200;
    lv_obj_t *wx = s_w[W_WEATHER];
    lv_obj_set_style_text_font(s_wx_icon, tiny ? g_icon_32 : g_icon_80, 0);
    lv_obj_set_style_text_font(s_wx_temp, tiny ? g_font_32 : g_font_48, 0);
    lv_obj_align(s_wx_icon, LV_ALIGN_LEFT_MID, tiny ? -2 : -4, 0);
    if (tiny) {
        lv_obj_align(s_wx_temp, LV_ALIGN_RIGHT_MID, 0, 0);
        lv_obj_add_flag(s_wx_text, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s_wx_range, LV_OBJ_FLAG_HIDDEN);
    } else {
        // Temperature, condition and range as one block, centred in the height
        // (the 48 px font's own top space puts the digits at the block's top).
        int block = UI_COMPACT ? 80 : 85;
        int y0 = (inner_height(wx, h) - block) / 2;
        if (y0 < 0) y0 = 0;
        lv_obj_set_align(s_wx_temp, LV_ALIGN_TOP_LEFT);
        lv_obj_set_pos(s_wx_temp, 96, y0 - 8);
        lv_obj_set_pos(s_wx_text, 98, y0 + (UI_COMPACT ? 42 : 46));
        lv_obj_set_pos(s_wx_range, 98, y0 + (UI_COMPACT ? 61 : 66));
        lv_obj_remove_flag(s_wx_text, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(s_wx_range, LV_OBJ_FLAG_HIDDEN);
    }
}

static void create_printer(lv_obj_t *page)
{
    lv_obj_t *c = widget_card(page, PAGE_PRINTER);
    s_pr_name = ui_caption(c, "3D PRINT");
    lv_obj_set_pos(s_pr_name, 0, 0);
    s_pr_state = ui_label(c, g_font_14, COL_MUTED, "");
    lv_obj_align(s_pr_state, LV_ALIGN_TOP_RIGHT, 0, 0);
    s_pr_file = ui_label(c, g_font_20, COL_TEXT, "");
    ui_marquee(s_pr_file);
    lv_obj_set_pos(s_pr_file, 0, 22);
    s_pr_bar = ui_bar(c, COL_ACCENT);
    lv_obj_set_height(s_pr_bar, 10);
    lv_obj_set_pos(s_pr_bar, 0, UI_COMPACT ? 50 : 54);
    s_pr_info = ui_label(c, g_font_14, COL_MUTED, "");
    lv_obj_set_pos(s_pr_info, 0, UI_COMPACT ? 64 : 70);
    s_w[W_PRINTER] = c;
}

static void layout_printer(int w, int h)
{
    // In the narrow column the printer name and state take the caption's place.
    if (w < 340) lv_obj_add_flag(s_pr_name, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_remove_flag(s_pr_name, LV_OBJ_FLAG_HIDDEN);
    lv_obj_align(s_pr_state, w < 340 ? LV_ALIGN_TOP_LEFT : LV_ALIGN_TOP_RIGHT, 0, 0);
    lv_obj_set_width(s_pr_file, w - 32);
    lv_obj_set_width(s_pr_bar, w - 32);
    // Caption on top, progress line on the bottom edge, file name and bar
    // centred between them.
    int inner = inner_height(s_w[W_PRINTER], h);
    int cap_h = lv_font_get_line_height(g_font_14), file_h = lv_font_get_line_height(g_font_20);
    int info_y = inner - cap_h;
    int block = file_h + 4 + 10;
    int top = cap_h + (info_y - cap_h - block) / 2;
    if (top < cap_h) top = cap_h;
    lv_obj_set_pos(s_pr_file, 0, top);
    lv_obj_set_pos(s_pr_bar, 0, top + file_h + 4);
    lv_obj_set_pos(s_pr_info, 0, info_y);
}

// ---- game (Steam) ---------------------------------------------------------
// The game being played, or the last one with a play button.
static lv_obj_t *s_gm_img, *s_gm_ph, *s_gm_cap, *s_gm_sub, *s_gm_name, *s_gm_play;
static uint32_t s_gm_appid;   // what the play button launches
static int s_gm_iw = 2, s_gm_ih = 3;   // shape of the cover shown (2:3 until one arrives)

static void game_play_cb(lv_event_t *e)
{
    (void)e;
    if (s_gm_appid) games_launch(s_gm_appid);
}

static void create_game(lv_obj_t *page)
{
    // A portrait cover at the card's full height, with what's playing, the play
    // time and the game's name stacked beside it. Sizes are set in layout_game().
    lv_obj_t *c = widget_card(page, PAGE_GAMES);
    s_gm_ph = lv_obj_create(c);
    lv_obj_remove_style_all(s_gm_ph);
    lv_obj_remove_flag(s_gm_ph, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_style_radius(s_gm_ph, 8, 0);
    lv_obj_set_style_bg_color(s_gm_ph, lv_color_hex(COL_CARD_HI), 0);
    lv_obj_set_style_bg_opa(s_gm_ph, LV_OPA_COVER, 0);
    lv_obj_t *pi = ui_label(s_gm_ph, g_icon_32, COL_DIM, ICON_SPORTS_ESPORTS);
    lv_obj_center(pi);
    s_gm_img = lv_image_create(c);
    lv_obj_remove_flag(s_gm_img, LV_OBJ_FLAG_CLICKABLE);
    lv_image_set_inner_align(s_gm_img, LV_IMAGE_ALIGN_STRETCH);
    lv_obj_set_style_radius(s_gm_img, 8, 0);
    lv_obj_set_style_clip_corner(s_gm_img, true, 0);
    s_gm_cap = ui_caption(c, "STEAM");
    ui_marquee(s_gm_cap);
    lv_obj_set_height(s_gm_cap, 18);
    s_gm_sub = ui_label(c, g_font_14, COL_MUTED, "");
    ui_marquee(s_gm_sub);
    lv_obj_set_height(s_gm_sub, 18);
    s_gm_name = ui_label(c, g_font_20, COL_TEXT, "");
    ui_marquee(s_gm_name);
    lv_obj_set_height(s_gm_name, 24);
    // Play button (last game, nothing running) sits on the cover.
    s_gm_play = ui_round_button(c, 40, COL_ACCENT, g_icon_24, ICON_PLAY_ARROW, game_play_cb, NULL);
    lv_obj_set_style_text_color(lv_obj_get_child(s_gm_play, 0), lv_color_hex(COL_BG), 0);
    lv_obj_set_style_bg_opa(s_gm_play, LV_OPA_90, 0);
    s_w[W_GAME] = c;
}

static void layout_game(int w, int h)
{
    lv_obj_t *c = s_w[W_GAME];
    if (h <= 0) h = lv_obj_get_height(c);
    int inner = h - lv_obj_get_style_pad_top(c, 0) - lv_obj_get_style_pad_bottom(c, 0) - 2;
    // Full height at the cover's own shape; a wide image is capped so the text
    // keeps room, and then shrinks in height instead of being squeezed.
    int tile_h = inner, tile_w = inner * s_gm_iw / s_gm_ih;
    int max_w = (w - 32) * 2 / 5;
    if (tile_w > max_w) {
        tile_w = max_w;
        tile_h = tile_w * s_gm_ih / s_gm_iw;
    }
    lv_obj_set_size(s_gm_ph, tile_w, tile_h);
    lv_obj_set_size(s_gm_img, tile_w, tile_h);
    lv_obj_set_pos(s_gm_ph, 0, (inner - tile_h) / 2);
    lv_obj_set_pos(s_gm_img, 0, (inner - tile_h) / 2);
    int x = tile_w + 14, text_w = w - 32 - x;
    int y0 = (inner - 66) / 2;   // the three lines, centred beside the cover
    lv_obj_set_pos(s_gm_cap, x, y0);
    lv_obj_set_pos(s_gm_sub, x, y0 + 20);
    lv_obj_set_pos(s_gm_name, x, y0 + 40);
    lv_obj_set_width(s_gm_cap, text_w);
    lv_obj_set_width(s_gm_sub, text_w);
    lv_obj_set_width(s_gm_name, text_w);
    lv_obj_align_to(s_gm_play, s_gm_ph, LV_ALIGN_CENTER, 0, 0);
}

// ---- internet ---------------------------------------------------------------
// Latency in big type, online status, and the last speed test.
static lv_obj_t *s_nt_state, *s_nt_lat, *s_nt_sub;

static void create_net(lv_obj_t *page)
{
    lv_obj_t *c = widget_card(page, PAGE_NETWORK);
    lv_obj_t *cap = ui_caption(c, "INTERNET");
    lv_obj_set_pos(cap, 0, 0);
    s_nt_state = ui_label(c, g_font_14, COL_GOOD, "");
    lv_obj_align(s_nt_state, LV_ALIGN_TOP_RIGHT, 0, 0);
    s_nt_lat = ui_label(c, g_font_32, COL_TEXT, "--");
    lv_obj_set_pos(s_nt_lat, 0, 22);
    s_nt_sub = ui_label(c, g_font_14, COL_MUTED, "");
    ui_marquee(s_nt_sub);
    lv_obj_set_height(s_nt_sub, 18);
    lv_obj_set_pos(s_nt_sub, 0, UI_COMPACT ? 62 : 66);
    s_w[W_NET] = c;
}

static void layout_net(int w, int h)
{
    // Caption on top, the speed line on the bottom edge, latency centred between.
    int inner = inner_height(s_w[W_NET], h);
    int cap_h = lv_font_get_line_height(g_font_14), lat_h = lv_font_get_line_height(g_font_32);
    int sub_y = inner - 18;
    int lat_y = cap_h + (sub_y - cap_h - lat_h) / 2;
    if (lat_y < cap_h) lat_y = cap_h;
    lv_obj_set_pos(s_nt_lat, 0, lat_y);
    lv_obj_set_pos(s_nt_sub, 0, sub_y);
    // One cell wide: the caption and latency; the status shows as the latency's colour.
    bool tiny = w < 200;
    if (tiny) {
        lv_obj_add_flag(s_nt_state, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s_nt_sub, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_remove_flag(s_nt_state, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(s_nt_sub, LV_OBJ_FLAG_HIDDEN);
    }
    lv_obj_set_width(s_nt_sub, w - 32);
}

// ---- GitHub contributions ----------------------------------------------------
// The squares from a GitHub profile (drawn by ui_github.c), as many weeks as the
// widget has room for. One row tall: the chart fills the height and the year's
// total and streak sit to its right. Two rows: month labels over the chart, the
// total in the caption row and today / this week / streak along the bottom.
// Tapping it opens the GitHub page.
#define GH_STATS 3
static lv_obj_t *s_gh_cap, *s_gh_total, *s_gh_chart, *s_gh_side_val, *s_gh_side_sub, *s_gh_side_priv;
static lv_obj_t *s_gh_month[HUB_GH_MONTHS];
static lv_obj_t *s_gh_stat_val[GH_STATS], *s_gh_stat_cap[GH_STATS];

static void create_github(lv_obj_t *page)
{
    lv_obj_t *c = widget_card(page, PAGE_GITHUB);
    s_gh_cap = ui_caption(c, "GITHUB");
    s_gh_total = ui_label(c, g_font_14, COL_MUTED, "");
    ui_marquee(s_gh_total);
    lv_obj_set_style_text_align(s_gh_total, LV_TEXT_ALIGN_RIGHT, 0);
    s_gh_chart = github_chart_create(c);
    for (int i = 0; i < HUB_GH_MONTHS; i++) s_gh_month[i] = ui_label(c, g_font_14, COL_DIM, "");
    static const char *const caps[GH_STATS] = {"today", "this week", "day streak"};
    for (int i = 0; i < GH_STATS; i++) {
        s_gh_stat_val[i] = ui_label(c, g_font_24, COL_TEXT, "--");
        s_gh_stat_cap[i] = ui_label(c, g_font_14, COL_MUTED, caps[i]);
    }
    s_gh_side_val = ui_label(c, g_font_24, COL_TEXT, "--");
    s_gh_side_sub = ui_label(c, g_font_14, COL_MUTED, "");
    ui_marquee(s_gh_side_sub);
    s_gh_side_priv = ui_label(c, g_font_14, COL_MUTED, "");
    ui_marquee(s_gh_side_priv);
    s_w[W_GITHUB] = c;
}

static void layout_github(int w, int h)
{
    lv_obj_t *c = s_w[W_GITHUB];
    int inner = inner_height(c, h), iw = w - 32;
    int lh14 = lv_font_get_line_height(g_font_14), lh24 = lv_font_get_line_height(g_font_24);
    bool tall = inner >= 150;
    int weeks_have = github_total_weeks();
    if (tall) {
        // Caption row on top, the stats row on the bottom edge, months and chart between.
        int stat_h = lh24 + lh14;
        int avail = inner - lh14 - stat_h;
        int pitch_h = (avail - 16 - lh14 - 4 + GH_GAP) / 7;
        int pitch_w = (iw + GH_GAP) / 53;
        if (pitch_w < 12) pitch_w = 12;
        int pitch = pitch_h < pitch_w ? pitch_h : pitch_w;
        if (pitch < 6) pitch = 6;
        int weeks = (iw + GH_GAP) / pitch;
        if (weeks > weeks_have) weeks = weeks_have;
        int cw = weeks * pitch - GH_GAP, ch = 7 * pitch - GH_GAP;
        int by = lh14 + (avail - (lh14 + 4 + ch)) / 2;
        int cx = (iw - cw) / 2;
        // The total (and private count) fills the caption row right of "GITHUB".
        lv_obj_set_pos(s_gh_cap, 0, 0);
        int cap_w = lv_text_get_width("GITHUB", 6, g_font_14, 2) + 16;
        lv_obj_set_size(s_gh_total, iw - cap_w, lh14);
        lv_obj_set_pos(s_gh_total, cap_w, 0);
        lv_obj_remove_flag(s_gh_total, LV_OBJ_FLAG_HIDDEN);
        github_chart_set(s_gh_chart, cx, by + lh14 + 4, pitch, weeks);
        github_place_months(s_gh_month, s_gh_chart, by, true);
        for (int i = 0; i < GH_STATS; i++) {
            int x = i * iw / GH_STATS;
            lv_obj_set_pos(s_gh_stat_val[i], x, inner - stat_h);
            lv_obj_set_pos(s_gh_stat_cap[i], x, inner - lh14);
            lv_obj_remove_flag(s_gh_stat_val[i], LV_OBJ_FLAG_HIDDEN);
            lv_obj_remove_flag(s_gh_stat_cap[i], LV_OBJ_FLAG_HIDDEN);
        }
        lv_obj_add_flag(s_gh_side_val, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s_gh_side_sub, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s_gh_side_priv, LV_OBJ_FLAG_HIDDEN);
    } else {
        // Chart on the left as tall as the card; caption, total and streak to its right.
        int tw = w >= 400 ? 128 : 100;
        int pitch = (inner + GH_GAP) / 7;
        int weeks = (iw - tw - 12 + GH_GAP) / pitch;
        if (weeks > weeks_have) weeks = weeks_have;
        if (weeks < 1) weeks = 1;
        github_chart_set(s_gh_chart, 0, (inner - (7 * pitch - GH_GAP)) / 2, pitch, weeks);
        github_place_months(s_gh_month, s_gh_chart, 0, false);
        // Caption, total, streak and (with private work) the private count, spread
        // from the top edge to the bottom one. Four lines take a smaller total when the
        // card is short (above the bottom bar).
        int tx = iw - tw;
        bool priv = g_github.valid && g_github.private_count > 0;
        const lv_font_t *vf = priv && inner < 3 * lh14 + lh24 + 6 ? g_font_20 : g_font_24;
        lv_obj_set_style_text_font(s_gh_side_val, vf, 0);
        lv_obj_t *rows[4] = {s_gh_cap, s_gh_side_val, s_gh_side_sub, s_gh_side_priv};
        int n = priv ? 4 : 3, used = 0;
        for (int i = 0; i < n; i++) {
            used += i == 1 ? lv_font_get_line_height(vf) : lh14;
            if (i >= 2) {
                lv_obj_set_size(rows[i], tw, lh14);
                lv_obj_remove_flag(rows[i], LV_OBJ_FLAG_HIDDEN);
            }
        }
        if (!priv) lv_obj_add_flag(s_gh_side_priv, LV_OBJ_FLAG_HIDDEN);
        int gap = (inner - used) / (n - 1), y = 0;
        for (int i = 0; i < n; i++) {
            lv_obj_set_pos(rows[i], tx, y);
            y += (i == 1 ? lv_font_get_line_height(vf) : lh14) + gap;
        }
        lv_obj_remove_flag(s_gh_side_val, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s_gh_total, LV_OBJ_FLAG_HIDDEN);
        for (int i = 0; i < GH_STATS; i++) {
            lv_obj_add_flag(s_gh_stat_val[i], LV_OBJ_FLAG_HIDDEN);
            lv_obj_add_flag(s_gh_stat_cap[i], LV_OBJ_FLAG_HIDDEN);
        }
    }
}

void home_refresh_github(void)
{
    if (!s_gh_chart) return;
    const hub_github_t *g = &g_github;
    char buf[80], n[16], priv[32] = "";
    if (!g->valid) {
        const char *why = g_pc_online ? "Waiting for the PC" : "PC offline";
        lv_label_set_text(s_gh_total, why);
        lv_label_set_text(s_gh_side_val, "--");
        lv_label_set_text(s_gh_side_sub, why);
        for (int i = 0; i < GH_STATS; i++) lv_label_set_text(s_gh_stat_val[i], "--");
    } else {
        // Contributions in private repositories, when there are any.
        if (g->private_count > 0) {
            github_fmt_count(n, sizeof(n), g->private_count);
            snprintf(priv, sizeof(priv), "  " "•" "  %s private", n);
        }
        github_fmt_count(n, sizeof(n), g->total);
        snprintf(buf, sizeof(buf), "%s in the last year%s", n, priv);
        lv_label_set_text(s_gh_total, buf);
        lv_label_set_text(s_gh_side_val, n);
        if (g->streak > 0) snprintf(buf, sizeof(buf), "%d-day streak", g->streak);
        else snprintf(buf, sizeof(buf), "%d today", g->today);
        lv_label_set_text(s_gh_side_sub, buf);
        github_fmt_count(n, sizeof(n), g->private_count);
        snprintf(buf, sizeof(buf), "%s private", n);
        lv_label_set_text(s_gh_side_priv, buf);
        const int vals[GH_STATS] = {g->today, g->week, g->streak};
        for (int i = 0; i < GH_STATS; i++) {
            github_fmt_count(n, sizeof(n), vals[i]);
            lv_label_set_text(s_gh_stat_val[i], n);
        }
    }
    lv_obj_t *card = s_w[W_GITHUB];
    // The number of weeks or months may have changed: lay out again at the card's size.
    if (card && !lv_obj_has_flag(card, LV_OBJ_FLAG_HIDDEN)) {
        layout_github(lv_obj_get_width(card), lv_obj_get_height(card));
    }
}

// Slim top bar: big time + AM/PM on the left, full date on the right, and a
// hairline underneath. Used instead of the clock widget when the spec starts
// with "header;".
static void create_header(lv_obj_t *page)
{
    s_hdr = lv_obj_create(page);
    lv_obj_remove_style_all(s_hdr);
    lv_obj_set_size(s_hdr, CONTENT_W, HEADER_H);
    lv_obj_set_pos(s_hdr, 0, 0);
    lv_obj_remove_flag(s_hdr, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_style_border_side(s_hdr, LV_BORDER_SIDE_BOTTOM, 0);
    lv_obj_set_style_border_color(s_hdr, lv_color_hex(COL_BORDER), 0);
    lv_obj_set_style_border_width(s_hdr, 1, 0);
    s_hdr_time = ui_label(s_hdr, ui_header_font(), COL_TEXT, "--:--");
    lv_obj_align(s_hdr_time, LV_ALIGN_LEFT_MID, 0, -4);
    s_hdr_ampm = ui_label(s_hdr, g_font_20, COL_MUTED, "");
    s_hdr_date = ui_label(s_hdr, g_font_24, COL_MUTED, "");
    lv_obj_align(s_hdr_date, LV_ALIGN_RIGHT_MID, 0, -2);
    lv_obj_add_flag(s_hdr, LV_OBJ_FLAG_HIDDEN);
}

static void update_header(const struct tm *tm, const char *time_text)
{
    if (strcmp(lv_label_get_text(s_hdr_time), time_text) != 0) {
        lv_label_set_text(s_hdr_time, time_text);
        lv_obj_update_layout(s_hdr_time);
    }
#if CONFIG_HUB_24H
    lv_label_set_text(s_hdr_ampm, "");
#else
    lv_label_set_text(s_hdr_ampm, tm->tm_hour < 12 ? "AM" : "PM");
#endif
    lv_obj_align_to(s_hdr_ampm, s_hdr_time, LV_ALIGN_OUT_RIGHT_BOTTOM, 8, -7);
    char d[40];
    strftime(d, sizeof(d), "%A, %B %e", tm);
    if (strcmp(lv_label_get_text(s_hdr_date), d) != 0) {
        lv_label_set_text(s_hdr_date, d);
        lv_obj_align(s_hdr_date, LV_ALIGN_RIGHT_MID, 0, -2);
    }
}

// ---- layout ---------------------------------------------------------------

static void layout_widget(int id, int w, int h)
{
    switch (id) {
    case W_CLOCK: layout_clock(w, h); break;
    case W_CLAUDE: layout_claude(w, h); break;
    case W_EVENTS: layout_events(w, h); break;
    case W_MEDIA: layout_media(w, h); break;
    case W_STATS: layout_stats(w, h); break;
    case W_PRINTER: layout_printer(w, h); break;
    case W_GAME: layout_game(w, h); break;
    case W_NET: layout_net(w, h); break;
    case W_GITHUB: layout_github(w, h); break;
    case W_WEATHER: layout_weather(w, h); break;
    default: break;
    }
}

static bool widget_enabled(int id)
{
    return W_PAGE[id] < 0 || (g_pages_mask & (1u << W_PAGE[id]));
}

static int parse_column(const char *s, int *ids)
{
    int n = 0;
    char name[16];
    while (*s && *s != '|' && n < W_COUNT) {
        int len = 0;
        while (s[len] && s[len] != ',' && s[len] != '|' && len < (int)sizeof(name) - 1) len++;
        memcpy(name, s, len);
        name[len] = 0;
        for (int i = 0; i < W_COUNT; i++) {
            if (strcmp(name, W_NAMES[i]) == 0) {
                ids[n++] = i;
                break;
            }
        }
        s += len;
        if (*s == ',') s++;
    }
    return n;
}

static void place_column(const int *ids, int n, int x, int w, bool *placed)
{
    int fixed = 0, flex = 0, count = 0;
    for (int i = 0; i < n; i++) {
        if (placed[ids[i]] || !widget_enabled(ids[i])) continue;
        count++;
        if (ids[i] == W_EVENTS) flex++;
        else fixed += widget_height(ids[i], w);
    }
    int free_h = CONTENT_H - s_top - fixed - (count > 0 ? (count - 1) * GAP : 0);
    int flex_h = flex ? free_h / flex : 0;
    int y = s_top;
    for (int i = 0; i < n; i++) {
        int id = ids[i];
        if (placed[id] || !widget_enabled(id)) continue;
        int h = id == W_EVENTS ? flex_h : widget_height(id, w);
        if (id == W_EVENTS && h < W_MIN_EVENTS) h = W_MIN_EVENTS;
        if (y + h > CONTENT_H) break; // the rest of this column doesn't fit
        lv_obj_set_pos(s_w[id], x, y);
        lv_obj_set_size(s_w[id], w, h);
        layout_widget(id, w, h);
        lv_obj_remove_flag(s_w[id], LV_OBJ_FLAG_HIDDEN);
        placed[id] = true;
        y += h + GAP;
    }
}

// ---- grid: 5 columns x 3 rows ------------------------------------------------
// "grid;[header;]weather@0,0,2,1;stats@0,1,2,1;..." places each widget on cells
// x,y (from the top left) spanning w x h. Cells share the space the navigation
// bar leaves (and the top bar, if shown), with GAP between them.
#define GRID_COLS 5
#define GRID_ROWS 3

static void place_grid(const char *s, bool *placed)
{
    int area_w = CONTENT_W, area_h = CONTENT_H - s_top;
    while (*s) {
        const char *end = strchr(s, ';');
        int len = end ? (int)(end - s) : (int)strlen(s);
        char item[32];
        if (len < (int)sizeof(item)) {
            memcpy(item, s, len);
            item[len] = 0;
            char *at = strchr(item, '@');
            int x, y, w, h;
            if (at && sscanf(at + 1, "%d,%d,%d,%d", &x, &y, &w, &h) == 4) {
                *at = 0;
                int id = -1;
                for (int i = 0; i < W_COUNT; i++) {
                    if (strcmp(item, W_NAMES[i]) == 0) id = i;
                }
                if (x < 0) x = 0;
                if (y < 0) y = 0;
                if (x + w > GRID_COLS) w = GRID_COLS - x;
                if (y + h > GRID_ROWS) h = GRID_ROWS - y;
                if (id >= 0 && !placed[id] && w > 0 && h > 0 && widget_enabled(id)) {
                    // Edges from the cell grid, so every widget lines up exactly.
                    int x0 = x * (area_w + GAP) / GRID_COLS;
                    int x1 = (x + w) * (area_w + GAP) / GRID_COLS - GAP;
                    int y0 = y * (area_h + GAP) / GRID_ROWS;
                    int y1 = (y + h) * (area_h + GAP) / GRID_ROWS - GAP;
                    lv_obj_set_pos(s_w[id], x0, s_top + y0);
                    lv_obj_set_size(s_w[id], x1 - x0, y1 - y0);
                    layout_widget(id, x1 - x0, y1 - y0);
                    lv_obj_remove_flag(s_w[id], LV_OBJ_FLAG_HIDDEN);
                    placed[id] = true;
                }
            }
        }
        if (!end) break;
        s = end + 1;
    }
}

// Re-place every widget from s_spec and the page toggles.
void home_apply_layout(void)
{
    if (!s_w[W_CLOCK]) return;
    bool placed[W_COUNT] = {0};
    int left[W_COUNT], right[W_COUNT];
    bool grid = strncmp(s_spec, "grid;", 5) == 0;
    const char *spec = grid ? s_spec + 5 : s_spec;
    // "header;" in front of the spec: time and date in a slim bar across the top
    // instead of the clock widget, which leaves room for more widgets.
    s_header = strncmp(spec, "header;", 7) == 0;
    const char *cols = s_header ? spec + 7 : spec;
    s_top = s_header ? HEADER_H + GAP : 0;
    if (s_header) {
        lv_obj_remove_flag(s_hdr, LV_OBJ_FLAG_HIDDEN);
        placed[W_CLOCK] = true;  // never placed in a column...
        lv_obj_add_flag(s_w[W_CLOCK], LV_OBJ_FLAG_HIDDEN);  // ...and hidden
    } else {
        lv_obj_add_flag(s_hdr, LV_OBJ_FLAG_HIDDEN);
    }
    if (grid) {
        place_grid(cols, placed);
    } else {
        // Two columns, from companions older than the grid.
        int nl = parse_column(cols, left);
        const char *bar = strchr(cols, '|');
        int nr = bar ? parse_column(bar + 1, right) : 0;
        place_column(left, nl, 0, LEFT_W, placed);
        place_column(right, nr, RIGHT_X, RIGHT_W, placed);
    }
    for (int i = 0; i < W_COUNT; i++) {
        if (!placed[i]) lv_obj_add_flag(s_w[i], LV_OBJ_FLAG_HIDDEN);
    }
    ui_alert_relayout();
    ui_calendar_badge();
}

bool home_shows_events(void)
{
    return s_w[W_EVENTS] && !lv_obj_has_flag(s_w[W_EVENTS], LV_OBJ_FLAG_HIDDEN);
}

void home_set_layout(const char *spec)
{
    if (!spec || strcmp(spec, s_spec) == 0) return;
    strlcpy(s_spec, spec, sizeof(s_spec));
    nvs_handle_t h;
    if (nvs_open("hub", NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_str(h, "home", s_spec);
        nvs_commit(h);
        nvs_close(h);
    }
    home_apply_layout();
}

void home_create(lv_obj_t *page)
{
    nvs_handle_t h;
    if (nvs_open("hub", NVS_READONLY, &h) == ESP_OK) {
        size_t len = sizeof(s_spec);
        char buf[sizeof(s_spec)];
        if (nvs_get_str(h, "home", buf, &len) == ESP_OK) strlcpy(s_spec, buf, sizeof(s_spec));
        nvs_close(h);
    }
    create_clock(page);
    create_header(page);
    create_claude(page);
    create_events(page);
    create_weather(page);
    create_media(page);
    create_stats(page);
    create_printer(page);
    create_game(page);
    create_net(page);
    create_github(page);
    home_apply_layout();

    home_refresh_events();
    home_refresh_media();
    home_refresh_stats();
    home_refresh_claude();
    home_refresh_printer();
    home_refresh_game();
    home_refresh_net();
    home_refresh_github();
}

// ---- refresh --------------------------------------------------------------

static void claude_limit(int i, bool has, float pct, time_t reset)
{
    char val[16], sub[40];
    if (!has) {
        lv_label_set_text(s_cl_val[i], "--");
        lv_obj_set_style_text_color(s_cl_val[i], lv_color_hex(COL_MUTED), 0);
        lv_bar_set_value(s_cl_bar[i], 0, LV_ANIM_OFF);
        lv_label_set_text(s_cl_sub[i], !g_pc_online ? "PC offline" : g_claude.valid ? "No data yet" : "Waiting for PC");
        return;
    }
    uint32_t color = ui_level_color(pct);
    snprintf(val, sizeof(val), "%d%%", (int)(pct + 0.5f));
    lv_label_set_text(s_cl_val[i], val);
    lv_obj_set_style_text_color(s_cl_val[i], lv_color_hex(color), 0);
    lv_bar_set_value(s_cl_bar[i], (int)(pct + 0.5f), LV_ANIM_ON);
    ui_bar_set_color(s_cl_bar[i], color);
    // An old reading says so instead of counting down, so it isn't taken as current.
    if (ui_limits_stale(g_claude.limits_updated)) ui_fmt_asof(sub, sizeof(sub), g_claude.limits_updated);
    else ui_fmt_countdown(sub, sizeof(sub), reset);
    lv_label_set_text(s_cl_sub[i], sub);
}

void home_refresh_claude(void)
{
    const hub_claude_t *c = &g_claude;
    if (!s_cl_icon) return;
    lv_image_set_src(s_cl_icon, ui_agent_logo(24));
    lv_obj_set_style_bg_color(s_cl_badge, lv_color_hex(ui_agent_color()), 0);
    claude_limit(0, c->valid && c->has_5h, c->pct_5h, c->reset_5h);
    claude_limit(1, c->valid && c->has_7d, c->pct_7d, c->reset_7d);
    if (c->valid && !c->has_5h && c->has_7d && strcmp(c->provider, "codex") == 0) {
        lv_label_set_text(s_cl_sub[0], "No 5-hour limit");
    } else if (c->valid && !c->has_5h && c->has_7d && !ui_limits_stale(c->limits_updated)) {
        // The 5-hour window expired; a new one starts at the next prompt (only
        // known from a recent reading).
        lv_label_set_text(s_cl_val[0], "0%");
        lv_obj_set_style_text_color(s_cl_val[0], lv_color_hex(COL_GOOD), 0);
        lv_label_set_text(s_cl_sub[0], "Fresh window ready");
    } else if (c->valid && !c->has_5h && c->has_7d) {
        char sub[40];
        ui_fmt_asof(sub, sizeof(sub), c->limits_updated);
        lv_label_set_text(s_cl_sub[0], sub);
    }
}

void home_tick(void)
{
    if (!net_time_valid()) return;
    time_t now = time(NULL);
    struct tm tm;
    localtime_r(&now, &tm);
    char buf[64];
#if CONFIG_HUB_24H
    snprintf(buf, sizeof(buf), "%02d:%02d", tm.tm_hour, tm.tm_min);
#else
    int h = tm.tm_hour % 12;
    snprintf(buf, sizeof(buf), "%d:%02d", h ? h : 12, tm.tm_min);
    lv_label_set_text(s_ampm, tm.tm_hour < 12 ? "AM" : "PM");
#endif
    if (s_header) {
        update_header(&tm, buf);
        return;  // the clock widget is hidden in this mode
    }
    if (strcmp(s_time, buf) != 0) {
        int digits_before = time_digits();
        strlcpy(s_time, buf, sizeof(s_time));
        if (time_digits() != digits_before) {
            home_apply_layout();  // e.g. 9:59 -> 10:00: new size, so the column may reflow
        } else {
            clock_geo_t g = clock_geo(lv_obj_get_width(s_w[W_CLOCK]), lv_obj_get_height(s_w[W_CLOCK]));
            set_clock_text(&g);
        }
    }
    update_date(&tm);
}

static int text_width(const char *s, const lv_font_t *f, int letter_space)
{
    lv_point_t sz;
    lv_text_get_size(&sz, s, f, letter_space, 0, LV_COORD_MAX, LV_TEXT_FLAG_NONE);
    return sz.x;
}

// The date line. In the wide column: the longest format that fits at 24 px.
// In the narrow column (under a clock that fills the width): the most complete
// format that still reads well, sized and letter-spaced to span the column.
static void update_date(const struct tm *tm)
{
    static const char *formats[] = {"%A, %B %e", "%A, %b %e", "%a, %B %e", "%a, %b %e", "%b %e"};
    const int nfmt = sizeof(formats) / sizeof(formats[0]);
    int w = lv_obj_get_width(s_w[W_CLOCK]);
    char text[48];
    const lv_font_t *ref = g_font_24;

    if (!s_clock_narrow) {
        // Wide column: AM/PM sits beside the clock; longest date that fits at 24 px.
        lv_obj_add_flag(s_date_dot, LV_OBJ_FLAG_HIDDEN);
        for (int i = 0; i < nfmt; i++) {
            strftime(text, sizeof(text), formats[i], tm);
            if (text_width(text, ref, 0) <= w) break;
        }
        lv_obj_set_style_text_font(s_ampm, g_font_24, 0);
        lv_obj_set_style_text_color(s_ampm, lv_color_hex(COL_MUTED), 0);
        if (s_date_px != 24 || strcmp(lv_label_get_text(s_date), text) != 0) {
            lv_obj_set_style_text_font(s_date, ref, 0);
            lv_obj_set_style_text_letter_space(s_date, 0, 0);
            lv_label_set_text(s_date, text);
            if (s_date_px != 24) {
                s_date_px = 24;
                home_apply_layout();
            }
        }
        return;
    }

    // Narrow column, under a clock that fills it: the date on the left and AM/PM
    // right-aligned at the column's edge, so the line spans the full width. The
    // size is the largest that fits the most complete format (>= 20 px).
#if CONFIG_HUB_24H
    const char *ampm = NULL;
#else
    const char *ampm = tm->tm_hour < 12 ? "AM" : "PM";
#endif
#define NEED(t, f) (text_width((t), (f), 0) + (ampm ? 18 + text_width(ampm, (f), 0) : 0))
    int px = 20;
    for (int i = 0; i < nfmt; i++) {
        strftime(text, sizeof(text), formats[i], tm);
        px = 24 * w / NEED(text, ref);
        if (px >= 20) break;
    }
    if (px > 34) px = 34;
    if (px != s_date_px || strcmp(lv_label_get_text(s_date), text) != 0) {
        lv_font_t *label_old = NULL, *tmp_old = NULL;
        lv_font_t *font = ui_date_font(px, &label_old);
        // Correct the 24 px estimate: shrink until it fits, then grow while it still fits.
        while (font && px > 12 && NEED(text, font) > w) {
            font = ui_date_font(--px, &tmp_old);
            if (tmp_old) lv_tiny_ttf_destroy(tmp_old);
        }
        while (font && px < 34) {
            lv_font_t *bigger = ui_date_font(px + 1, &tmp_old);
            if (bigger && NEED(text, bigger) <= w) {
                if (tmp_old) lv_tiny_ttf_destroy(tmp_old);
                font = bigger;
                px++;
                continue;
            }
            lv_font_t *back_old = NULL;
            font = ui_date_font(px, &back_old);  // back to the size that fit
            if (back_old) lv_tiny_ttf_destroy(back_old);
            if (tmp_old && tmp_old != font) lv_tiny_ttf_destroy(tmp_old);
            break;
        }
        const lv_font_t *use = font ? font : ref;
        lv_obj_set_style_text_font(s_date, use, 0);
        lv_obj_set_style_text_letter_space(s_date, 0, 0);
        lv_obj_set_style_text_font(s_ampm, use, 0);
        lv_obj_set_style_text_color(s_ampm, lv_color_hex(COL_MUTED), 0);
        lv_label_set_text(s_date, text);
        if (label_old) lv_tiny_ttf_destroy(label_old);  // the size shown until now
        int shown_px = font ? px : 24;
        if (shown_px != s_date_px) {
            s_date_px = shown_px;
            home_apply_layout();  // the date's height changed
        }
    }
#undef NEED
    if (ampm) {
        lv_label_set_text(s_ampm, ampm);
        lv_obj_remove_flag(s_ampm, LV_OBJ_FLAG_HIDDEN);
        lv_obj_update_layout(s_ampm);
        lv_obj_set_pos(s_ampm, w - lv_obj_get_width(s_ampm), lv_obj_get_y(s_date));
        // Dot centred in the gap between the end of the date and AM/PM.
        int date_end = text_width(lv_label_get_text(s_date), lv_obj_get_style_text_font(s_date, 0), 0);
        int gap_mid = (date_end + lv_obj_get_x(s_ampm)) / 2;
        lv_obj_set_pos(s_date_dot, gap_mid - 2, lv_obj_get_y(s_date) + lv_obj_get_height(s_date) / 2 - 1);
        lv_obj_remove_flag(s_date_dot, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(s_ampm, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s_date_dot, LV_OBJ_FLAG_HIDDEN);
    }
}

void home_refresh_events(void)
{
    time_t now = time(NULL);
    int shown = 0;
    for (int i = 0; i < g_event_count && shown < s_ev_max; i++) {
        const hub_event_t *e = &g_events[i];
        if (e->end <= now) continue;
        lv_obj_remove_flag(s_ev_rows[shown], LV_OBJ_FLAG_HIDDEN);
        lv_obj_set_style_bg_color(s_ev_dot[shown], lv_color_hex(e->color), 0);
        lv_label_set_text(s_ev_title[shown], e->title);

        char when[48], t[16];
        struct tm st, nt;
        localtime_r(&e->start, &st);
        localtime_r(&now, &nt);
        const char *day = "";
        if (st.tm_yday == nt.tm_yday && st.tm_year == nt.tm_year) day = "Today";
        else if ((st.tm_yday == nt.tm_yday + 1 && st.tm_year == nt.tm_year) ||
                 (st.tm_yday == 0 && nt.tm_mon == 11 && nt.tm_mday == 31)) day = "Tomorrow";
        char daybuf[16];
        if (!day[0]) {
            strftime(daybuf, sizeof(daybuf), "%a %b %e", &st);
            day = daybuf;
        }
        if (e->all_day) {
            snprintf(when, sizeof(when), "%s  \xE2\x80\xA2  All day", day);
        } else if (e->start <= now) {
            ui_fmt_clock(t, sizeof(t), e->end);
            snprintf(when, sizeof(when), "Now  \xE2\x80\xA2  until %s", t);
        } else {
            ui_fmt_clock(t, sizeof(t), e->start);
            snprintf(when, sizeof(when), "%s  \xE2\x80\xA2  %s", day, t);
        }
        lv_label_set_text(s_ev_time[shown], when);
        shown++;
    }
    for (int i = shown; i < EVENT_ROWS_MAX; i++) {
        lv_obj_add_flag(s_ev_rows[i], LV_OBJ_FLAG_HIDDEN);
    }

    // The line under the rows: how many more there are, or that there are none.
    int more = 0;
    for (int i = 0; i < g_event_count; i++) {
        if (g_events[i].end > now) more++;
    }
    more -= shown;
    char span[24], msg[64];
    if (g_event_days == 1) snprintf(span, sizeof(span), "the next day");
    else snprintf(span, sizeof(span), "the next %d days", g_event_days);
    if (more > 0) snprintf(msg, sizeof(msg), "%d more in %s", more, span);
    else if (shown) snprintf(msg, sizeof(msg), "No more events in %s", span);
    else snprintf(msg, sizeof(msg), "Nothing scheduled in %s", span);
    lv_label_set_text(s_ev_empty, msg);
    int y = 26 + shown * EV_PITCH + 2;
    lv_obj_set_pos(s_ev_empty, shown ? 14 : 0, y);
    if (y + lv_font_get_line_height(g_font_16) <= s_ev_inner)
        lv_obj_remove_flag(s_ev_empty, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_add_flag(s_ev_empty, LV_OBJ_FLAG_HIDDEN);
}

void home_refresh_weather(void)
{
    if (!g_weather.valid) return;
    char buf[48], hi[12], lo[12];
    lv_label_set_text(s_wx_icon, weather_icon(g_weather.code, g_weather.is_day));
    lv_obj_set_style_text_color(s_wx_icon, lv_color_hex(weather_color(g_weather.code, g_weather.is_day)), 0);
    ui_fmt_temp(buf, sizeof(buf), g_weather.temp);
    lv_label_set_text(s_wx_temp, buf);
    lv_label_set_text(s_wx_text, weather_text(g_weather.code));
    ui_fmt_temp(hi, sizeof(hi), g_weather.day_hi[0]);
    ui_fmt_temp(lo, sizeof(lo), g_weather.day_lo[0]);
    snprintf(buf, sizeof(buf), "H %s   L %s", hi, lo);
    lv_label_set_text(s_wx_range, buf);
}

void home_refresh_media(void)
{
    bool server = home_shows_server();
    const hub_media_t *m = server ? &g_server.now : &g_media;
    bool active = m->active;
    lv_label_set_text(s_md_title, active ? m->title : "Nothing playing");
    lv_label_set_text(s_md_artist, active ? m->artist : (g_pc_online ? "" : "PC offline"));
    lv_label_set_text(lv_obj_get_child(s_md_btn, 0), m->playing ? ICON_PAUSE : ICON_PLAY_ARROW);
    if (active && !m->no_controls) lv_obj_remove_flag(s_md_btn, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_add_flag(s_md_btn, LV_OBJ_FLAG_HIDDEN);

    bool has_art = server ? ui_server_has_art() : ui_media_has_art();
    // A server poster is 2:3: narrower, centred in the square frame.
    lv_obj_set_size(s_md_art, server ? 43 : 64, 64);
    lv_obj_set_x(s_md_art, server ? 10 : 0);
    if (has_art) {
        lv_image_set_src(s_md_art, server ? &g_srv_art : &g_art);
        lv_obj_invalidate(s_md_art);
        lv_obj_remove_flag(s_md_art, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s_md_ph, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(s_md_art, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(s_md_ph, LV_OBJ_FLAG_HIDDEN);
    }
}

void home_refresh_stats(void)
{
    float ram = g_stats.ram_total > 0 ? 100.0f * g_stats.ram_used / g_stats.ram_total : 0;
    float vals[3] = {g_stats.cpu, g_stats.has_gpu ? g_stats.gpu : 0, ram};
    for (int i = 0; i < 3; i++) {
        char buf[12];
        if (!g_pc_online || (i == 1 && !g_stats.has_gpu)) {
            lv_bar_set_value(s_st_bar[i], 0, LV_ANIM_OFF);
            lv_label_set_text(s_st_val[i], "--");
            continue;
        }
        lv_bar_set_value(s_st_bar[i], (int)vals[i], LV_ANIM_ON);
        snprintf(buf, sizeof(buf), "%d%%", (int)(vals[i] + 0.5f));
        lv_label_set_text(s_st_val[i], buf);
    }
}

void home_refresh_printer(void)
{
    if (!s_pr_file) return;
    // The printer that is printing, else the first one.
    const hub_printer_t *p = NULL;
    for (int i = 0; i < g_printer_count; i++) {
        const char *st = g_printers[i].state;
        if (!strcmp(st, "RUNNING") || !strcmp(st, "PAUSE") || !strcmp(st, "PREPARE")) {
            p = &g_printers[i];
            break;
        }
    }
    if (!p && g_printer_count) p = &g_printers[0];
    if (!p) {
        lv_label_set_text(s_pr_state, "");
        lv_label_set_text(s_pr_file, "No printer connected");
        lv_bar_set_value(s_pr_bar, 0, LV_ANIM_OFF);
        lv_label_set_text(s_pr_info, "");
        return;
    }
    bool running = !strcmp(p->state, "RUNNING") || !strcmp(p->state, "PAUSE") || !strcmp(p->state, "PREPARE");
    bool done = !strcmp(p->state, "FINISH");
    uint32_t color = !strcmp(p->state, "PAUSE") ? COL_WARN : !strcmp(p->state, "FAILED") ? COL_BAD
                     : done ? COL_GOOD : running ? COL_ACCENT : COL_DIM;
    char buf[64];
    snprintf(buf, sizeof(buf), "%s  \xE2\x80\xA2  %s", p->name[0] ? p->name : p->model,
             !strcmp(p->state, "RUNNING") ? "Printing" : !strcmp(p->state, "PAUSE") ? "Paused"
             : !strcmp(p->state, "PREPARE") ? "Preparing" : done ? "Finished"
             : !strcmp(p->state, "FAILED") ? "Failed" : !strcmp(p->state, "OFFLINE") ? "Offline" : "Idle");
    lv_label_set_text(s_pr_state, buf);
    lv_obj_set_style_text_color(s_pr_state, lv_color_hex(color), 0);
    lv_label_set_text(s_pr_file, p->file[0] ? p->file : (running ? "Printing" : "Not printing"));
    int pct = done ? 100 : (running ? p->pct : 0);
    lv_bar_set_value(s_pr_bar, pct, LV_ANIM_ON);
    ui_bar_set_color(s_pr_bar, color);
    if (running && p->remaining_min > 0) {
        int m = p->remaining_min;
        if (m >= 60) snprintf(buf, sizeof(buf), "%d%%  \xE2\x80\xA2  %dh %02dm left", pct, m / 60, m % 60);
        else snprintf(buf, sizeof(buf), "%d%%  \xE2\x80\xA2  %dm left", pct, m);
    } else if (done) {
        snprintf(buf, sizeof(buf), "Done  \xE2\x80\xA2  ready to remove");
    } else {
        buf[0] = 0;
    }
    lv_label_set_text(s_pr_info, buf);
}

void home_refresh_game(void)
{
    if (!s_gm_name) return;
    const hub_steam_t *s = &g_steam;
    const hub_game_t *g = s->playing ? &s->now : s->recent_count ? &s->recent[0] : NULL;
    char buf[48], tot[16];
    s_gm_appid = (!s->playing && g) ? g->appid : 0;
    if (s_gm_appid) lv_obj_remove_flag(s_gm_play, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_add_flag(s_gm_play, LV_OBJ_FLAG_HIDDEN);

    uint32_t thumb_id = 0;
    const lv_image_dsc_t *thumb = games_thumb(&thumb_id);
    if (g && thumb && thumb_id == g->appid) {
        lv_image_set_src(s_gm_img, thumb);
        s_gm_iw = thumb->header.w;
        s_gm_ih = thumb->header.h;
        lv_obj_remove_flag(s_gm_img, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(s_gm_img, LV_OBJ_FLAG_HIDDEN);
    }

    if (!g) {
        lv_label_set_text(s_gm_cap, "STEAM");
        lv_obj_set_style_text_color(s_gm_cap, lv_color_hex(COL_MUTED), 0);
        lv_label_set_text(s_gm_sub, !s->valid ? (g_pc_online ? "Waiting for the PC" : "PC offline")
                                    : s->found ? "No games played yet" : "Steam not found");
        lv_label_set_text(s_gm_name, "");
    } else if (s->playing) {
        lv_label_set_text(s_gm_cap, "PLAYING");
        lv_obj_set_style_text_color(s_gm_cap, lv_color_hex(COL_GOOD), 0);
        int t = games_session_s();
        if (t >= 3600) snprintf(buf, sizeof(buf), "%dh %02dm", t / 3600, (t / 60) % 60);
        else snprintf(buf, sizeof(buf), "%dm", t / 60);
        if (games_total_worth_showing(g->total_min, t)) {
            ui_fmt_playtime(tot, sizeof(tot), g->total_min);
            strlcat(buf, "  " "•" "  ", sizeof(buf));
            strlcat(buf, tot, sizeof(buf));
        }
        if (strcmp(lv_label_get_text(s_gm_sub), buf) != 0) lv_label_set_text(s_gm_sub, buf);
        lv_label_set_text(s_gm_name, g->name);
    } else {
        lv_label_set_text(s_gm_cap, "RECENT");
        lv_obj_set_style_text_color(s_gm_cap, lv_color_hex(COL_MUTED), 0);
        games_fmt_ago(buf, sizeof(buf), g->last);
        if (g->total_min > 0) {
            ui_fmt_playtime(tot, sizeof(tot), g->total_min);
            strlcat(buf, "  " "•" "  ", sizeof(buf));
            strlcat(buf, tot, sizeof(buf));
        }
        lv_label_set_text(s_gm_sub, buf);
        lv_label_set_text(s_gm_name, g->name);
    }
    lv_obj_t *card = s_w[W_GAME];
    if (card && !lv_obj_has_flag(card, LV_OBJ_FLAG_HIDDEN)) layout_game(lv_obj_get_width(card), 0);
}

void home_refresh_net(void)
{
    if (!s_nt_lat) return;
    const hub_net_t *n = &g_net;
    char buf[64], t[24];
    if (!n->valid) {
        lv_label_set_text(s_nt_state, "");
        lv_label_set_text(s_nt_lat, "--");
        lv_label_set_text(s_nt_sub, g_pc_online ? "Waiting for the PC" : "PC offline");
        return;
    }
    lv_label_set_text(s_nt_state, n->online ? "Online" : "Offline");
    lv_obj_set_style_text_color(s_nt_state, lv_color_hex(n->online ? COL_GOOD : COL_BAD), 0);
    if (n->latency_ms >= 0) snprintf(buf, sizeof(buf), "%d ms", (int)(n->latency_ms + 0.5f));
    else strlcpy(buf, "--", sizeof(buf));
    lv_label_set_text(s_nt_lat, buf);
    lv_obj_set_style_text_color(s_nt_lat, lv_color_hex(n->latency_ms < 0 ? COL_DIM : n->latency_ms < 40 ? COL_TEXT
                                                       : n->latency_ms < 100 ? COL_WARN : COL_BAD), 0);
    if (n->testing) {
        strlcpy(buf, "Speed test running...", sizeof(buf));
    } else if (n->has_speed) {
        ui_fmt_ago(t, sizeof(t), n->speed_at);
        snprintf(buf, sizeof(buf), "↓" " %.0f  " "↑" " %.0f Mbps  " "•" "  %s",
                 n->down_mbps, n->up_mbps, t);
    } else {
        snprintf(buf, sizeof(buf), "Jitter %.1f ms  " "•" "  Loss %d%%", n->jitter_ms, n->loss_pct);
    }
    lv_label_set_text(s_nt_sub, buf);
}

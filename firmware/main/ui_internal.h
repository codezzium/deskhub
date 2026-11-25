// Shared between the UI pages.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <time.h>
#include "hub_model.h"
#include "lvgl.h"

// Graphite theme: a flat near-black ground, flat cards with a hairline edge, one
// warm amber accent for actions and highlights, and cool data colours for series.
// No gradients or transparency: plain fills are the cheapest thing to redraw.
#define COL_BG       0x0d0e11   // screen
#define COL_RAIL     0x111216   // navigation bar
#define COL_CARD     0x16181d   // card surface
#define COL_CARD_LO  COL_CARD
#define COL_CARD_HI  0x262a32   // pressed card, selected tab, empty bar track
#define COL_BORDER   0x262a32   // hairline card edge
#define COL_PRESSED  0x30343d   // pressed neutral button
#define COL_TEXT     0xf3f4f6
#define COL_MUTED    0x9aa0aa   // secondary text (7:1 on a card)
#define COL_DIM      0x6b717c   // idle icons and placeholders, not body text
#define COL_ACCENT   0xffb224   // amber: actions, the current item, highlights
#define COL_ON_ACCENT 0x16181d  // text and icons on amber
#define COL_ACCENT_PRESSED 0xd9931a
#define COL_ACCENT2  0x6cb4ff   // blue: the first data series
#define COL_CYAN     0x5ed3c6   // teal: the second data series
#define COL_DATA3    0xb49cff   // lilac: the third data series
#define COL_GOOD     0x3dd68c
#define COL_WARN     0xffb224
#define COL_BAD      0xff6b6b

// Layout: navigation rail on the left, pages to its right.
// The navigation bar sits on the left (default), the right or along the bottom.
// Read from NVS at boot; pages are built for the space that leaves.
enum { NAV_LEFT, NAV_RIGHT, NAV_BOTTOM };
extern int g_nav;
#define RAIL_W 88                      // side rail width
#define DOCK_H 68                      // bottom dock height
#define PAGE_W (g_nav == NAV_BOTTOM ? 800 : 800 - RAIL_W)   // 712 beside the rail
#define PAGE_H (g_nav == NAV_BOTTOM ? 480 - DOCK_H : 480)   // 412 above the dock
#define PAD 20
#define CONTENT_W (PAGE_W - 2 * PAD)   // 672 with a side rail, 760 with the dock
#define CONTENT_H (PAGE_H - 2 * PAD)   // 440 with a side rail, 372 with the dock

// Material Icons and the large clock font, rendered at runtime by Tiny TTF.
extern lv_font_t *g_icon_20, *g_icon_24, *g_icon_32, *g_icon_48, *g_icon_80, *g_font_clock;
// Text: Inter at fixed sizes (Medium up to 24 px, SemiBold at 32 and 48).
extern lv_font_t *g_font_14, *g_font_16, *g_font_20, *g_font_24, *g_font_28, *g_font_32, *g_font_48;

// Latest data, for pages that show it.
extern hub_stats_t g_stats;
extern hub_media_t g_media;
extern hub_weather_t g_weather;
extern hub_event_t g_events[HUB_MAX_EVENTS];
extern int g_event_count;
extern int g_event_days;   // how many days ahead the events cover
extern lv_image_dsc_t g_art;     // valid when g_art_id != 0
extern uint32_t g_art_id;
extern hub_server_t g_server;
extern lv_image_dsc_t g_srv_art; // valid when g_srv_art_id != 0
extern uint32_t g_srv_art_id;
extern bool g_pc_online;

lv_obj_t *ui_card(lv_obj_t *parent);
lv_obj_t *ui_label(lv_obj_t *parent, const lv_font_t *font, uint32_t color, const char *text);
lv_obj_t *ui_bar(lv_obj_t *parent, uint32_t color);
// Text too long for its label (which needs a fixed width) rests, slides slowly
// to show the end, rests, and slides back. Use instead of LONG_DOT / SCROLL_CIRCULAR.
void ui_marquee(lv_obj_t *label);
void ui_bar_set_color(lv_obj_t *bar, uint32_t color);
lv_obj_t *ui_caption(lv_obj_t *parent, const char *text);   // small spaced-out section title
// Stack rows down a card: first at the top, last on the bottom edge, equal gaps.
void ui_spread(lv_obj_t *card, lv_obj_t *const *rows, int n);
lv_obj_t *ui_round_button(lv_obj_t *parent, int size, uint32_t bg, const lv_font_t *icon_font,
                          const char *glyph, lv_event_cb_t cb, void *user);
void ui_fmt_clock(char *buf, size_t n, time_t t);   // "3:05 PM" / "15:05"
void ui_fmt_hour(char *buf, size_t n, time_t t);    // "3 PM" / "15:00"
void ui_fmt_temp(char *buf, size_t n, float t);     // "72°"
void ui_fmt_duration(char *buf, size_t n, int s);   // "3:07"
void ui_utf8_glyph(char out[5], uint32_t cp);
const char *ui_icon_by_name(const char *name, char out[5]);
bool ui_media_has_art(void);
bool ui_server_has_art(void);
void ui_send_server(const char *cmd, int value);
void ui_show_page(int page);

enum { PAGE_HOME, PAGE_STATS, PAGE_CLAUDE, PAGE_CALENDAR, PAGE_WEATHER, PAGE_MEDIA, PAGE_PRINTER, PAGE_SHORTCUTS, PAGE_TIMER, PAGE_GAMES, PAGE_NETWORK,
       PAGE_GITHUB, PAGE_COUNT };

// Pages shown in the rail (bit per PAGE_*). Home is always on.
extern uint32_t g_pages_mask;
extern hub_printer_t g_printers[HUB_MAX_PRINTERS];
extern int g_printer_count;
void printer_create(lv_obj_t *page);
void printer_refresh(void);
void home_apply_layout(void);
void home_set_layout(const char *spec);   // "clock,claude,events|weather,media,stats"
void home_refresh_printer(void);
lv_font_t *ui_clock_font(int px, lv_font_t **old);
lv_font_t *ui_date_font(int px, lv_font_t **old);   // NULL if the font could not be made
lv_font_t *ui_header_font(void);
const lv_image_dsc_t *ui_agent_logo(int size);  // 24 or 32 px, for the agent in g_claude
const char *ui_agent_name(void);                // "Claude Code" / "Codex"
uint32_t ui_agent_color(void);                  // brand accent for charts and badges

extern hub_claude_t g_claude;
void claude_create(lv_obj_t *page);
void claude_refresh(void);
void claude_tick(void);
void home_refresh_claude(void);
void ui_fmt_tokens(char *buf, size_t n, double t);      // "1.2M"
void ui_fmt_countdown(char *buf, size_t n, time_t reset); // "Resets in 2h 14m"
// Plan limits measured more than 10 minutes ago are old: they can be well off.
bool ui_limits_stale(time_t updated);
void ui_fmt_asof(char *buf, size_t n, time_t updated);    // "as of 3:05 PM" / "as of Mon"
uint32_t ui_level_color(float pct);

void home_create(lv_obj_t *page);
void home_tick(void);
void home_refresh_weather(void);
void home_refresh_media(void);
void home_refresh_stats(void);
void home_refresh_events(void);

void stats_create(lv_obj_t *page);
void stats_refresh(void);
void dash_create(lv_obj_t *scr);
void dash_refresh(void);
void dash_mark_data(void);

void media_create(lv_obj_t *page);
void media_refresh(void);
void media_refresh_art(void);
void media_tick(void);
void media_refresh_server(void);
void media_refresh_server_art(void);

void calendar_create(lv_obj_t *page);
// force = rebuild even if the visible events look unchanged.
void calendar_refresh(bool force);

void weather_page_create(lv_obj_t *page);
void weather_page_refresh(void);

void shortcuts_create(lv_obj_t *page);
void shortcuts_refresh(const hub_tile_t *tiles, int n);
void shortcuts_set_online(bool online);

// Send a media command ("toggle", "next", "prev", "seek") to the PC.
void ui_send_media(const char *cmd, int value);

void timer_page_create(lv_obj_t *page);
lv_font_t *ui_timer_font(void);
// Show text (e.g. a countdown) on a rail tab instead of its icon; NULL restores the icon.
void ui_rail_set_text(int page, const char *text);

// Cards the "agent waiting" alert is shown over (NULL until created).
lv_obj_t *home_claude_card(void);
lv_obj_t *claude_history_card(void);
// A small coloured dot on a rail tab; color 0 removes it.
void ui_rail_set_badge(int page, uint32_t color);
// The Calendar tab's dot: an event still to come today, when Home doesn't show Up next.
void ui_calendar_badge(void);
bool home_shows_events(void);
void ui_alert_relayout(void);   // after Home re-lays out its widgets
void shortcuts_set_lit(uint32_t mask);

extern hub_steam_t g_steam;
void games_page_create(lv_obj_t *page);
void games_refresh(void);
void games_tick(void);
int games_session_s(void);                          // live seconds into the current session
const lv_image_dsc_t *games_thumb(uint32_t *appid); // Home thumbnail, NULL if none
void games_launch(uint32_t appid);
void ui_fmt_playtime(char *buf, size_t n, int minutes);  // "312 h" / "45 min"
void home_refresh_game(void);
void games_fmt_ago(char *buf, size_t n, time_t t);    // "Yesterday", "3 days ago"
bool games_total_worth_showing(int total_min, int session_s);

extern hub_net_t g_net;
void net_page_create(lv_obj_t *page);
void net_refresh(void);
void ui_fmt_bytes(char *buf, size_t n, double bytes);   // "3.2 GB"
void ui_fmt_ago(char *buf, size_t n, time_t t);         // "5m ago", "2h ago"
void home_refresh_net(void);
extern hub_github_t g_github;
void home_refresh_github(void);
// GitHub contribution chart, shared by the Home widget and the GitHub page.
#define GH_GAP 2   // between squares
lv_obj_t *github_chart_create(lv_obj_t *parent);
// Squares from (x, y) in the parent, pitch px apart, the newest `weeks` columns.
void github_chart_set(lv_obj_t *chart, int x, int y, int pitch, int weeks);
int github_total_weeks(void);
// Month names over the chart's columns at row y (HUB_GH_MONTHS labels); show false hides them.
void github_place_months(lv_obj_t *const *labels, lv_obj_t *chart, int y, bool show);
void github_fmt_count(char *buf, size_t n, int v);   // "1,019"
void github_page_create(lv_obj_t *page);
void github_page_refresh(void);
void net_refresh_live(void);   // the "Now" line, on each stats message
void stats_set_view(int view);          // 0 = PC, 1 = Internet
void stats_set_net_enabled(bool on);
#define UI_COMPACT (CONTENT_H < 400)   // the bottom dock leaves 372 px: pages use tighter layouts

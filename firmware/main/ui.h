// Public UI API. Every function must be called with display_lock() held.
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "hub_model.h"

void ui_init(void);

void ui_set_stats(const hub_stats_t *s);
bool dash_pc_gone(void);
void ui_set_media(const hub_media_t *m);
void ui_set_art(uint32_t id, uint16_t w, uint16_t h, const uint8_t *rgb565);
// Plex / Jellyfin section of the Media page, and its poster (separate from the PC's cover).
void ui_set_server(const hub_server_t *s);
void ui_set_server_art(uint32_t id, uint16_t w, uint16_t h, const uint8_t *rgb565);
// days: how far ahead the companion looked for events.
void ui_set_events(const hub_event_t *ev, int n, int days);
void ui_set_tiles(const hub_tile_t *tiles, int n);
void ui_set_weather(const hub_weather_t *w);
void ui_set_claude(const hub_claude_t *c);
void ui_set_printers(const hub_printer_t *p, int n);
// Bit i set = page i shown (see the PAGE_* order in ui_internal.h). Saved to NVS.
void ui_set_layout(uint32_t pages_mask);
// Navigation bar order: page indices (PAGE_*), first tab first. Home always leads;
// pages not listed follow in the default order. Saved to NVS.
void ui_set_page_order(const uint8_t *order, int n);
// Home widgets per column, e.g. "clock,claude,events|weather,media,stats". Saved to NVS.
void ui_set_home(const char *spec);
// Full-screen "agent waiting" alert; a->active false hides it.
void ui_set_alert(const hub_alert_t *a);
// Shortcut tiles that are "on" (bit i = tile i), e.g. mic or speaker muted.
void ui_set_tile_state(uint32_t on_mask);
// Steam: what is playing and the recent games, plus their cover art.
// kind: 0 = header of the playing game, 1 = Home thumbnail, 2 = portrait cover.
void ui_set_steam(const hub_steam_t *s);
void ui_set_steam_art(uint32_t appid, int kind, uint16_t w, uint16_t h, const uint8_t *rgb565);
void ui_set_net(const hub_net_t *n);
// GitHub contribution chart (a Home widget).
void ui_set_github(const hub_github_t *g);
// Navigation bar position: 0 left, 1 right, 2 bottom. Saved to NVS; restarts the board on change.
void ui_set_nav(int nav);
// Screensaver (a drifting clock) shown while the PC's display is asleep. A tap hides it.
void ui_saver_show(bool on);
bool ui_saver_shown(void);
// Firmware update prompt (Install / Deny), its progress, and a final message.
void ui_update_show(void);
void ui_update_progress(int pct);
void ui_update_message(const char *text);
void ui_update_hide(void);

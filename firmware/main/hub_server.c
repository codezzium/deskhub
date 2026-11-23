#include "hub_server.h"

#include <stdlib.h>
#include <math.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "board.h"
#include "cJSON.h"
#include "esp_lcd_panel_rgb.h"
#include "display.h"
#include "esp_heap_caps.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "hub_model.h"
#include "net.h"
#include "ota.h"
#include "sdkconfig.h"
#include "ui.h"

static const char *TAG = "server";

#define MAX_FRAME (300 * 1024)
#define ONLINE_TIMEOUT_MS 5000
#define PREAUTH_MAX 256
#define ART_MAGIC "ART1"

static httpd_handle_t s_server;
static volatile int s_client = -1;
static volatile uint32_t s_last_rx_ms; // 32-bit so cross-core reads are atomic

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

// ---- helpers ---------------------------------------------------------------

static void copy_str(char *dst, size_t n, const cJSON *obj, const char *key)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(obj, key);
    if (cJSON_IsString(v) && v->valuestring) {
        strlcpy(dst, v->valuestring, n);
    } else {
        dst[0] = 0;
    }
}

static double get_num(const cJSON *obj, const char *key, double fallback)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(obj, key);
    return cJSON_IsNumber(v) ? v->valuedouble : fallback;
}

static bool get_bool(const cJSON *obj, const char *key)
{
    return cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(obj, key));
}

static uint32_t get_color(const cJSON *obj, const char *key, uint32_t fallback)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(obj, key);
    if (cJSON_IsString(v) && v->valuestring && v->valuestring[0] == '#') {
        return (uint32_t)strtoul(v->valuestring + 1, NULL, 16);
    }
    return fallback;
}

// ---- message handlers ------------------------------------------------------

static void on_stats(const cJSON *m)
{
    hub_stats_t s = {0};
    s.cpu = get_num(m, "cpu", 0);
    s.cpu_temp = get_num(m, "cpu_temp", -1);
    s.ram_used = get_num(m, "ram_used", 0);
    s.ram_total = get_num(m, "ram_total", 0);
    s.has_gpu = cJSON_IsNumber(cJSON_GetObjectItemCaseSensitive(m, "gpu"));
    s.gpu = get_num(m, "gpu", 0);
    s.gpu_temp = get_num(m, "gpu_temp", -1);
    s.vram_used = get_num(m, "vram_used", 0);
    s.vram_total = get_num(m, "vram_total", 0);
    s.net_down = get_num(m, "net_down", 0);
    s.net_up = get_num(m, "net_up", 0);
    s.disk = get_num(m, "disk", 0);
    copy_str(s.cpu_name, sizeof(s.cpu_name), m, "cpu_name");
    copy_str(s.gpu_name, sizeof(s.gpu_name), m, "gpu_name");
    display_lock();
    ui_set_stats(&s);
    display_unlock();
}

static void on_media(const cJSON *m)
{
    hub_media_t md = {0};
    md.active = get_bool(m, "active");
    md.playing = get_bool(m, "playing");
    md.can_seek = get_bool(m, "can_seek");
    md.no_controls = cJSON_IsFalse(cJSON_GetObjectItemCaseSensitive(m, "controls"));
    md.pos = (int)get_num(m, "pos", 0);
    md.dur = (int)get_num(m, "dur", 0);
    md.art_id = (uint32_t)get_num(m, "art_id", 0);
    copy_str(md.title, sizeof(md.title), m, "title");
    copy_str(md.artist, sizeof(md.artist), m, "artist");
    copy_str(md.album, sizeof(md.album), m, "album");
    copy_str(md.source, sizeof(md.source), m, "source");
    display_lock();
    ui_set_media(&md);
    display_unlock();
}

static void on_events(const cJSON *m)
{
    static hub_event_t ev[HUB_MAX_EVENTS];
    int n = 0;
    const cJSON *arr = cJSON_GetObjectItemCaseSensitive(m, "events");
    const cJSON *it;
    cJSON_ArrayForEach(it, arr) {
        if (n >= HUB_MAX_EVENTS) break;
        hub_event_t *e = &ev[n++];
        memset(e, 0, sizeof(*e));
        e->start = (time_t)get_num(it, "start", 0);
        e->end = (time_t)get_num(it, "end", 0);
        e->all_day = get_bool(it, "all_day");
        e->color = get_color(it, "color", 0x7c9cff);
        copy_str(e->title, sizeof(e->title), it, "title");
        copy_str(e->location, sizeof(e->location), it, "loc");
        copy_str(e->calendar, sizeof(e->calendar), it, "cal");
    }
    display_lock();
    ui_set_events(ev, n, (int)get_num(m, "days", 7));
    display_unlock();
}

static void on_tiles(const cJSON *m)
{
    static hub_tile_t tiles[HUB_MAX_TILES];
    int n = 0;
    const cJSON *arr = cJSON_GetObjectItemCaseSensitive(m, "tiles");
    const cJSON *it;
    cJSON_ArrayForEach(it, arr) {
        if (n >= HUB_MAX_TILES) break;
        hub_tile_t *t = &tiles[n++];
        t->color = get_color(it, "color", 0x2a2f3a);
        copy_str(t->label, sizeof(t->label), it, "label");
        copy_str(t->icon, sizeof(t->icon), it, "icon");
    }
    display_lock();
    ui_set_tiles(tiles, n);
    display_unlock();
}

static void on_claude(const cJSON *m)
{
    static hub_claude_t c;
    memset(&c, 0, sizeof(c));
    c.valid = true;
    copy_str(c.provider, sizeof(c.provider), m, "provider");
    if (!c.provider[0]) strlcpy(c.provider, "claude", sizeof(c.provider));

    const cJSON *limits = cJSON_GetObjectItemCaseSensitive(m, "limits");
    const cJSON *w5 = cJSON_GetObjectItemCaseSensitive(limits, "five_hour");
    const cJSON *w7 = cJSON_GetObjectItemCaseSensitive(limits, "seven_day");
    c.limits_updated = (time_t)get_num(limits, "updated", 0);
    if (cJSON_IsObject(w5)) {
        c.has_5h = true;
        c.pct_5h = get_num(w5, "pct", 0);
        c.reset_5h = (time_t)get_num(w5, "reset", 0);
    }
    if (cJSON_IsObject(w7)) {
        c.has_7d = true;
        c.pct_7d = get_num(w7, "pct", 0);
        c.reset_7d = (time_t)get_num(w7, "reset", 0);
    }

    const cJSON *block = cJSON_GetObjectItemCaseSensitive(m, "block");
    c.block_active = get_bool(block, "active");
    c.block_reset = (time_t)get_num(block, "reset", 0);
    c.block_tokens = get_num(block, "tokens", 0);
    c.block_msgs = (int)get_num(block, "messages", 0);

    const cJSON *today = cJSON_GetObjectItemCaseSensitive(m, "today");
    c.today_tokens = get_num(today, "tokens", 0);
    c.today_output = get_num(today, "output", 0);
    c.today_msgs = (int)get_num(today, "messages", 0);
    c.last_hour = get_num(m, "last_hour", 0);

    int i = 0;
    const cJSON *it;
    cJSON_ArrayForEach(it, cJSON_GetObjectItemCaseSensitive(m, "days")) {
        if (i >= HUB_CLAUDE_DAYS) break;
        c.days[i++] = cJSON_IsNumber(it) ? it->valuedouble : 0;
    }
    cJSON_ArrayForEach(it, cJSON_GetObjectItemCaseSensitive(m, "models")) {
        if (c.model_count >= HUB_CLAUDE_MODELS) break;
        copy_str(c.model_name[c.model_count], sizeof(c.model_name[0]), it, "name");
        c.model_tokens[c.model_count] = get_num(it, "tokens", 0);
        c.model_count++;
    }
    display_lock();
    ui_set_claude(&c);
    display_unlock();
}

static float get_temp(const cJSON *obj, const char *key)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(obj, key);
    return cJSON_IsNumber(v) ? (float)v->valuedouble : NAN;
}

static void on_printer(const cJSON *m)
{
    static hub_printer_t ps[HUB_MAX_PRINTERS];
    int n = 0;
    const cJSON *it;
    cJSON_ArrayForEach(it, cJSON_GetObjectItemCaseSensitive(m, "printers")) {
        if (n >= HUB_MAX_PRINTERS) break;
        hub_printer_t *p = &ps[n++];
        memset(p, 0, sizeof(*p));
        p->online = get_bool(it, "online");
        copy_str(p->name, sizeof(p->name), it, "name");
        copy_str(p->model, sizeof(p->model), it, "model");
        copy_str(p->state, sizeof(p->state), it, "state");
        copy_str(p->file, sizeof(p->file), it, "file");
        copy_str(p->speed, sizeof(p->speed), it, "speed");
        copy_str(p->error, sizeof(p->error), it, "error");
        p->pct = (int)get_num(it, "pct", 0);
        p->remaining_min = (int)get_num(it, "remaining_min", 0);
        p->layer = (int)get_num(it, "layer", 0);
        p->layers = (int)get_num(it, "layers", 0);
        p->nozzle = get_temp(it, "nozzle");
        p->nozzle_target = get_temp(it, "nozzle_target");
        p->bed = get_temp(it, "bed");
        p->bed_target = get_temp(it, "bed_target");
        p->chamber = get_temp(it, "chamber");
        const cJSON *slot;
        cJSON_ArrayForEach(slot, cJSON_GetObjectItemCaseSensitive(it, "ams")) {
            if (p->ams_count >= HUB_MAX_AMS) break;
            int i = p->ams_count++;
            p->ams_color[i] = get_color(slot, "color", 0x444444);
            copy_str(p->ams_type[i], sizeof(p->ams_type[i]), slot, "type");
            p->ams_active[i] = get_bool(slot, "active");
        }
    }
    display_lock();
    ui_set_printers(ps, n);
    display_unlock();
}

// Which pages to show: {"type":"layout","pages":["home","stats",...]}.
static void on_layout(const cJSON *m)
{
    static const char *names[] = {"home", "stats", "ai", "calendar", "weather", "media", "printer", "shortcuts", "timer", "games", "network",
                                  "github"};
    uint32_t mask = 1; // Home is always shown
    uint8_t order[32];
    int n = 0;
    const cJSON *it;
    cJSON_ArrayForEach(it, cJSON_GetObjectItemCaseSensitive(m, "pages")) {
        if (!cJSON_IsString(it)) continue;
        for (int i = 0; i < (int)(sizeof(names) / sizeof(names[0])); i++) {
            if (strcmp(it->valuestring, names[i]) == 0) {
                if (!(mask & (1u << i)) && n < (int)sizeof(order)) order[n++] = (uint8_t)i;
                mask |= 1u << i;
            }
        }
    }
    display_lock();
    ui_set_layout(mask);
    // "ordered": true means "pages" is in navigation bar order (older companions
    // sent a fixed order).
    if (get_bool(m, "ordered")) ui_set_page_order(order, n);
    // "nav": "left" | "right" | "bottom" (restarts the board when it changes).
    char nav[8];
    copy_str(nav, sizeof(nav), m, "nav");
    if (!strcmp(nav, "left")) ui_set_nav(0);
    else if (!strcmp(nav, "right")) ui_set_nav(1);
    else if (!strcmp(nav, "bottom")) ui_set_nav(2);
    display_unlock();
    // "pc_sleep": what the board does while the PC's display sleeps: "off" | "clock" | "none".
    char sleep[8];
    copy_str(sleep, sizeof(sleep), m, "pc_sleep");
    if (!strcmp(sleep, "none")) display_set_pc_sleep_mode(PC_SLEEP_NONE);
    else if (!strcmp(sleep, "clock")) display_set_pc_sleep_mode(PC_SLEEP_CLOCK);
    else if (!strcmp(sleep, "off")) display_set_pc_sleep_mode(PC_SLEEP_OFF);
    // Night mode: "night_start"/"night_end" in minutes after midnight (-1 = off),
    // "night_style": "off" | "clock". Only companions that know about it send it.
    if (cJSON_GetObjectItemCaseSensitive(m, "night_start")) {
        char style[8];
        copy_str(style, sizeof(style), m, "night_style");
        display_set_night((int)get_num(m, "night_start", -1), (int)get_num(m, "night_end", -1),
                          strcmp(style, "clock") == 0 ? PC_SLEEP_CLOCK : PC_SLEEP_OFF);
    }
}

// The PC's display went to sleep or woke: {"type":"pc_power","asleep":true}.
static void on_pc_power(const cJSON *m)
{
    display_set_pc_asleep(cJSON_IsTrue(cJSON_GetObjectItem(m, "asleep")));
}

// Home widgets: {"type":"home","header":true,"grid":[{"widget":"weather","x":0,"y":0,"w":2,"h":1},...]}
// or, from older companions, "left"/"right" columns of widget names.
static void on_home(const cJSON *m)
{
    char spec[256] = "";
    const cJSON *grid = cJSON_GetObjectItemCaseSensitive(m, "grid");
    if (cJSON_IsArray(grid)) strlcpy(spec, "grid;", sizeof(spec));
    if (get_bool(m, "header")) strlcat(spec, "header;", sizeof(spec));
    if (cJSON_IsArray(grid)) {
        const cJSON *it;
        cJSON_ArrayForEach(it, grid) {
            char name[16], item[40];
            copy_str(name, sizeof(name), it, "widget");
            if (!name[0]) continue;
            snprintf(item, sizeof(item), "%s@%d,%d,%d,%d;", name, (int)get_num(it, "x", 0), (int)get_num(it, "y", 0),
                     (int)get_num(it, "w", 1), (int)get_num(it, "h", 1));
            strlcat(spec, item, sizeof(spec));
        }
    } else {
        const char *cols[2] = {"left", "right"};
        for (int c = 0; c < 2; c++) {
            if (c) strlcat(spec, "|", sizeof(spec));
            bool first = true;
            const cJSON *it;
            cJSON_ArrayForEach(it, cJSON_GetObjectItemCaseSensitive(m, cols[c])) {
                if (!cJSON_IsString(it) || strlen(it->valuestring) > 12) continue;
                if (!first) strlcat(spec, ",", sizeof(spec));
                strlcat(spec, it->valuestring, sizeof(spec));
                first = false;
            }
        }
    }
    display_lock();
    ui_set_home(spec);
    display_unlock();
}

static void copy_game(hub_game_t *g, const cJSON *o)
{
    g->appid = (uint32_t)get_num(o, "appid", 0);
    copy_str(g->name, sizeof(g->name), o, "name");
    g->last = (time_t)get_num(o, "last", 0);
    g->total_min = (int)get_num(o, "total_min", 0);
}

// Steam: {"type":"steam","found":true,"playing":{...}|null,"recent":[{...}]}.
static void on_steam(const cJSON *m)
{
    static hub_steam_t s;
    memset(&s, 0, sizeof(s));
    s.valid = true;
    s.found = get_bool(m, "found");
    const cJSON *p = cJSON_GetObjectItemCaseSensitive(m, "playing");
    if (cJSON_IsObject(p)) {
        s.playing = true;
        copy_game(&s.now, p);
        s.session_s = (int)get_num(p, "session_s", 0);
    }
    const cJSON *it;
    cJSON_ArrayForEach(it, cJSON_GetObjectItemCaseSensitive(m, "recent")) {
        if (s.recent_count >= HUB_STEAM_RECENT) break;
        copy_game(&s.recent[s.recent_count++], it);
    }
    cJSON_ArrayForEach(it, cJSON_GetObjectItemCaseSensitive(m, "friends")) {
        if (s.friend_count >= HUB_STEAM_FRIENDS) break;
        copy_str(s.friend_name[s.friend_count], sizeof(s.friend_name[0]), it, "name");
        copy_str(s.friend_game[s.friend_count], sizeof(s.friend_game[0]), it, "game");
        s.friend_count++;
    }
    display_lock();
    ui_set_steam(&s);
    display_unlock();
}

// Internet: {"type":"net","online":true,"latency_ms":14.2,"hist":[...],"speed":{...},...}.
static void on_net(const cJSON *m)
{
    static hub_net_t n;
    memset(&n, 0, sizeof(n));
    n.valid = true;
    n.online = get_bool(m, "online");
    n.latency_ms = (float)get_num(m, "latency_ms", -1);
    n.jitter_ms = (float)get_num(m, "jitter_ms", 0);
    n.loss_pct = (int)get_num(m, "loss_pct", 0);
    const cJSON *it;
    cJSON_ArrayForEach(it, cJSON_GetObjectItemCaseSensitive(m, "hist")) {
        if (n.hist_count >= HUB_NET_HIST) break;
        n.hist[n.hist_count++] = cJSON_IsNumber(it) ? (int16_t)it->valueint : -1;
    }
    n.up_since = (time_t)get_num(m, "up_since", 0);
    const cJSON *drop = cJSON_GetObjectItemCaseSensitive(m, "last_drop");
    n.drop_at = (time_t)get_num(drop, "at", 0);
    n.drop_dur_s = (int)get_num(drop, "dur_s", 0);
    n.today_bytes = get_num(m, "today_bytes", 0);
    n.month_bytes = get_num(m, "month_bytes", 0);
    const cJSON *sp = cJSON_GetObjectItemCaseSensitive(m, "speed");
    n.has_speed = cJSON_IsObject(sp);
    n.down_mbps = (float)get_num(sp, "down_mbps", 0);
    n.up_mbps = (float)get_num(sp, "up_mbps", 0);
    n.speed_ping_ms = (float)get_num(sp, "ping_ms", 0);
    n.speed_at = (time_t)get_num(sp, "at", 0);
    n.testing = get_bool(m, "testing");
    copy_str(n.ip, sizeof(n.ip), m, "ip");
    display_lock();
    ui_set_net(&n);
    display_unlock();
}

// GitHub: {"type":"github","levels":"0012...","start_wday":0,"months":[[2,"Dec"],...],"total":..}.
static void on_github(const cJSON *m)
{
    static hub_github_t g;
    memset(&g, 0, sizeof(g));
    g.valid = true;
    g.accent = get_bool(m, "accent");
    g.total = (int)get_num(m, "total", 0);
    g.today = (int)get_num(m, "today", 0);
    g.week = (int)get_num(m, "week", 0);
    g.last_week = (int)get_num(m, "last_week", 0);
    g.streak = (int)get_num(m, "streak", 0);
    g.best = (int)get_num(m, "best", 0);
    g.start_wday = (int)get_num(m, "start_wday", 0) % 7;
    int shift = 0;   // week columns dropped from the front
    const cJSON *lv = cJSON_GetObjectItemCaseSensitive(m, "levels");
    if (cJSON_IsString(lv) && lv->valuestring) {
        const char *s = lv->valuestring;
        size_t n = strlen(s);
        // Keep the newest days if there are ever more than fit.
        if (n > HUB_GH_DAYS) {
            int drop = (int)(n - HUB_GH_DAYS);
            s += drop;
            shift = (g.start_wday + drop) / 7;
            g.start_wday = (g.start_wday + drop) % 7;
            n = HUB_GH_DAYS;
        }
        for (size_t i = 0; i < n; i++) g.level[i] = s[i] >= '0' && s[i] <= '4' ? s[i] - '0' : 0;
        g.day_count = (int)n;
    }
    const cJSON *it;
    cJSON_ArrayForEach(it, cJSON_GetObjectItemCaseSensitive(m, "months")) {
        if (g.month_count >= HUB_GH_MONTHS) break;
        const cJSON *col = cJSON_GetArrayItem(it, 0), *name = cJSON_GetArrayItem(it, 1);
        if (!cJSON_IsNumber(col) || !cJSON_IsString(name)) continue;
        if (col->valueint - shift < 0) continue;
        g.month_col[g.month_count] = (int16_t)(col->valueint - shift);
        strlcpy(g.month_name[g.month_count], name->valuestring, sizeof(g.month_name[0]));
        g.month_count++;
    }
    copy_str(g.login, sizeof(g.login), m, "login");
    g.longest = (int)get_num(m, "longest", 0);
    copy_str(g.best_date, sizeof(g.best_date), m, "best_date");
    cJSON_ArrayForEach(it, cJSON_GetObjectItemCaseSensitive(m, "week_days")) {
        if (g.week_day_count >= 7) break;
        g.week_days[g.week_day_count++] = cJSON_IsNumber(it) ? it->valueint : 0;
    }
    int lw = 0;
    cJSON_ArrayForEach(it, cJSON_GetObjectItemCaseSensitive(m, "last_week_days")) {
        if (lw >= 7) break;
        g.last_week_days[lw++] = cJSON_IsNumber(it) ? it->valueint : 0;
    }
    const cJSON *kinds = cJSON_GetObjectItemCaseSensitive(m, "kinds");
    g.commits = (int)get_num(kinds, "commits", 0);
    g.prs = (int)get_num(kinds, "prs", 0);
    g.reviews = (int)get_num(kinds, "reviews", 0);
    g.issues = (int)get_num(kinds, "issues", 0);
    g.private_count = (int)get_num(kinds, "private", 0);
    cJSON_ArrayForEach(it, cJSON_GetObjectItemCaseSensitive(m, "repos")) {
        if (g.repo_count >= HUB_GH_REPOS) break;
        copy_str(g.repo_name[g.repo_count], sizeof(g.repo_name[0]), it, "name");
        g.repo_commits[g.repo_count] = (int)get_num(it, "count", 0);
        g.repo_count++;
    }
    display_lock();
    ui_set_github(&g);
    display_unlock();
}

// Toggle tiles that are on: {"type":"tile_state","on":[0,1]}.
static void on_tile_state(const cJSON *m)
{
    uint32_t mask = 0;
    const cJSON *it;
    cJSON_ArrayForEach(it, cJSON_GetObjectItemCaseSensitive(m, "on")) {
        if (cJSON_IsNumber(it) && it->valueint >= 0 && it->valueint < 32) mask |= 1u << it->valueint;
    }
    display_lock();
    ui_set_tile_state(mask);
    display_unlock();
}

// Agent waiting for the user: {"type":"alert","id":..,"kind":..,"title":..} or {"type":"alert","clear":true}.
static void on_alert(const cJSON *m)
{
    static hub_alert_t a;
    memset(&a, 0, sizeof(a));
    a.active = !get_bool(m, "clear");
    copy_str(a.id, sizeof(a.id), m, "id");
    // The id is echoed back inside JSON, so keep only safe characters.
    for (char *p = a.id; *p; p++) {
        bool ok = (*p >= '0' && *p <= '9') || (*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') ||
                  *p == '-' || *p == '_';
        if (!ok) *p = '_';
    }
    copy_str(a.kind, sizeof(a.kind), m, "kind");
    copy_str(a.agent, sizeof(a.agent), m, "agent");
    copy_str(a.title, sizeof(a.title), m, "title");
    copy_str(a.detail, sizeof(a.detail), m, "detail");
    copy_str(a.project, sizeof(a.project), m, "project");
    display_lock();
    ui_set_alert(&a);
    display_unlock();
}

static void on_text(int fd, char *text)
{
    cJSON *m = cJSON_Parse(text);
    if (!m) {
        ESP_LOGW(TAG, "bad json");
        return;
    }
    char type[24];
    copy_str(type, sizeof(type), m, "type");

    if (strcmp(type, "hello") == 0) {
        char token[64];
        copy_str(token, sizeof(token), m, "token");
        if (strcmp(token, CONFIG_HUB_TOKEN) == 0) {
            if (s_client >= 0 && s_client != fd) {
                httpd_sess_trigger_close(s_server, s_client);
            }
            s_client = fd;
            s_last_rx_ms = now_ms();
            ESP_LOGI(TAG, "companion connected (fd %d)", fd);
            hub_server_send("{\"type\":\"hello_ok\",\"version\":\"0.1.0\"}");
        } else {
            ESP_LOGW(TAG, "wrong token from fd %d", fd);
            httpd_sess_trigger_close(s_server, fd);
        }
    } else if (fd != s_client) {
        // Ignore everything from sockets that have not said hello.
    } else {
        s_last_rx_ms = now_ms();
        if (strcmp(type, "stats") == 0) on_stats(m);
        else if (strcmp(type, "media") == 0) on_media(m);
        else if (strcmp(type, "events") == 0) on_events(m);
        else if (strcmp(type, "tiles") == 0) on_tiles(m);
        else if (strcmp(type, "ai") == 0 || strcmp(type, "claude") == 0) on_claude(m);
        else if (strcmp(type, "printer") == 0) on_printer(m);
        else if (strcmp(type, "layout") == 0) on_layout(m);
        else if (strcmp(type, "pc_power") == 0) on_pc_power(m);
        else if (strcmp(type, "home") == 0) on_home(m);
        else if (strcmp(type, "alert") == 0) on_alert(m);
        else if (strcmp(type, "tile_state") == 0) on_tile_state(m);
        else if (strcmp(type, "steam") == 0) on_steam(m);
        else if (strcmp(type, "net") == 0) on_net(m);
        else if (strcmp(type, "github") == 0) on_github(m);
        else if (strcmp(type, "ping") == 0) { /* keeps the link marked online */ }
        else ESP_LOGD(TAG, "unknown type %s", type);
    }
    cJSON_Delete(m);
}

// Cover art: "ART1" | u32 art_id | u16 w | u16 h | w*h RGB565 little endian.
// Steam cover: "STM1" | u32 appid | u16 w | u16 h | u16 kind | u16 0 | w*h RGB565.
static void on_steam_art(const uint8_t *buf, size_t len)
{
    if (len < 16) return;
    uint32_t id = buf[4] | buf[5] << 8 | buf[6] << 16 | (uint32_t)buf[7] << 24;
    uint16_t w = buf[8] | buf[9] << 8;
    uint16_t h = buf[10] | buf[11] << 8;
    uint16_t kind = buf[12] | buf[13] << 8;
    if (kind > 2 || w == 0 || h == 0 || w > 400 || h > 240 || len != 16 + (size_t)w * h * 2) {
        ESP_LOGW(TAG, "bad steam art %ux%u kind %u len %u", w, h, kind, (unsigned)len);
        return;
    }
    s_last_rx_ms = now_ms();
    display_lock();
    ui_set_steam_art(id, kind, w, h, buf + 16);
    display_unlock();
}

static void on_binary(const uint8_t *buf, size_t len)
{
    if (len >= 4 && memcmp(buf, "STM1", 4) == 0) {
        on_steam_art(buf, len);
        return;
    }
    if (len < 12 || memcmp(buf, ART_MAGIC, 4) != 0) return;
    uint32_t id = buf[4] | buf[5] << 8 | buf[6] << 16 | (uint32_t)buf[7] << 24;
    uint16_t w = buf[8] | buf[9] << 8;
    uint16_t h = buf[10] | buf[11] << 8;
    if (w == 0 || h == 0 || w > 320 || h > 320 || len != 12 + (size_t)w * h * 2) {
        ESP_LOGW(TAG, "bad art frame %ux%u len %u", w, h, (unsigned)len);
        return;
    }
    s_last_rx_ms = now_ms();
    display_lock();
    ui_set_art(id, w, h, buf + 12);
    display_unlock();
}

// ---- http / ws plumbing ----------------------------------------------------

static esp_err_t ws_handler(httpd_req_t *req)
{
    if (req->method == HTTP_GET) {
        return ESP_OK; // handshake done
    }
    httpd_ws_frame_t frame = {0};
    esp_err_t err = httpd_ws_recv_frame(req, &frame, 0);
    if (err != ESP_OK) return err;
    if (frame.len == 0 || frame.len > MAX_FRAME) return frame.len ? ESP_FAIL : ESP_OK;
    // Until a socket says hello, only accept a frame big enough for the hello itself.
    if (httpd_req_to_sockfd(req) != s_client && frame.len > PREAUTH_MAX) return ESP_FAIL;

    uint8_t *buf = heap_caps_malloc(frame.len + 1, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!buf) return ESP_ERR_NO_MEM;
    frame.payload = buf;
    err = httpd_ws_recv_frame(req, &frame, frame.len);
    if (err == ESP_OK) {
        buf[frame.len] = 0;
        int fd = httpd_req_to_sockfd(req);
        if (frame.type == HTTPD_WS_TYPE_TEXT) {
            on_text(fd, (char *)buf);
        } else if (frame.type == HTTPD_WS_TYPE_BINARY && fd == s_client) {
            on_binary(buf, frame.len);
        }
    }
    free(buf);
    return err;
}

static esp_err_t root_handler(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/plain");
    return httpd_resp_sendstr(req, hub_server_pc_online() ? "Desk Hub: companion online\n"
                                                         : "Desk Hub: waiting for companion\n");
}

// ---- debug endpoints: /screenshot and /tap, both need ?token= --------------

static bool token_ok(httpd_req_t *req, char *query, size_t qlen)
{
    char token[64];
    if (httpd_req_get_url_query_str(req, query, qlen) != ESP_OK ||
        httpd_query_key_value(query, "token", token, sizeof(token)) != ESP_OK ||
        strcmp(token, CONFIG_HUB_TOKEN) != 0) {
        httpd_resp_send_err(req, HTTPD_403_FORBIDDEN, "bad token");
        return false;
    }
    return true;
}

static void put32(uint8_t *p, uint32_t v)
{
    p[0] = v; p[1] = v >> 8; p[2] = v >> 16; p[3] = v >> 24;
}

// Returns the current frame as a 24-bit BMP.
static esp_err_t screenshot_handler(httpd_req_t *req)
{
    char query[128];
    if (!token_ok(req, query, sizeof(query))) return ESP_OK;

    const int W = BOARD_WIDTH, H = BOARD_HEIGHT;
    size_t fb_bytes = (size_t)W * H * 2;
    uint16_t *copy = heap_caps_malloc(fb_bytes, MALLOC_CAP_SPIRAM);
    if (!copy) return httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "no memory");
    void *fb = NULL;
    // Copy under the LVGL lock so the frame isn't half-rendered.
    display_lock();
    esp_lcd_rgb_panel_get_frame_buffer(board_panel(), 1, &fb);
    if (fb) memcpy(copy, fb, fb_bytes);
    display_unlock();
    if (!fb) {
        free(copy);
        return httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "no frame buffer");
    }

    uint8_t hdr[54] = {'B', 'M'};
    uint32_t row = W * 3, img = row * H;
    put32(hdr + 2, 54 + img);
    put32(hdr + 10, 54);
    put32(hdr + 14, 40);
    put32(hdr + 18, W);
    put32(hdr + 22, H);
    hdr[26] = 1;
    hdr[28] = 24;
    put32(hdr + 34, img);
    httpd_resp_set_type(req, "image/bmp");
    httpd_resp_send_chunk(req, (const char *)hdr, sizeof(hdr));

    enum { ROWS = 16 };
    uint8_t *buf = malloc(row * ROWS);
    esp_err_t err = buf ? ESP_OK : ESP_ERR_NO_MEM;
    // BMP rows go bottom-up, pixels as B, G, R.
    for (int y = H - 1; y >= 0 && err == ESP_OK; y -= ROWS) {
        int n = 0;
        for (int r = 0; r < ROWS && y - r >= 0; r++, n++) {
            const uint16_t *src = copy + (size_t)(y - r) * W;
            uint8_t *dst = buf + (size_t)r * row;
            for (int x = 0; x < W; x++) {
                uint16_t p = src[x];
                dst[3 * x + 0] = (p & 0x1f) << 3;
                dst[3 * x + 1] = ((p >> 5) & 0x3f) << 2;
                dst[3 * x + 2] = (p >> 11) << 3;
            }
        }
        err = httpd_resp_send_chunk(req, (const char *)buf, row * n);
    }
    free(buf);
    free(copy);
    if (err == ESP_OK) httpd_resp_send_chunk(req, NULL, 0);
    return err;
}

static esp_err_t tap_handler(httpd_req_t *req)
{
    char query[128], v[8];
    if (!token_ok(req, query, sizeof(query))) return ESP_OK;
    int x = -1, y = -1;
    if (httpd_query_key_value(query, "x", v, sizeof(v)) == ESP_OK) x = atoi(v);
    if (httpd_query_key_value(query, "y", v, sizeof(v)) == ESP_OK) y = atoi(v);
    int x2 = x;
    if (httpd_query_key_value(query, "x2", v, sizeof(v)) == ESP_OK) x2 = atoi(v);
    if (x < 0 || y < 0 || x >= BOARD_WIDTH || y >= BOARD_HEIGHT) {
        return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "x/y out of range");
    }
    if (x2 < 0 || x2 >= BOARD_WIDTH) x2 = x;
    if (x2 == x) display_inject_tap(x, y);
    else display_inject_drag(x, y, x2, 250);
    return httpd_resp_sendstr(req, "ok\n");
}

// Rendering performance. ?full=1 also times one forced full-screen redraw.
static esp_err_t perf_handler(httpd_req_t *req)
{
    char query[128], v[4];
    if (!token_ok(req, query, sizeof(query))) return ESP_OK;
    display_perf_t p = display_perf();
    long full_us = -1;
    if (httpd_query_key_value(query, "full", v, sizeof(v)) == ESP_OK && v[0] == '1') {
        display_lock();
        full_us = (long)display_time_full_redraw();
        display_unlock();
    }
    char buf[200];
    snprintf(buf, sizeof(buf),
             "{\"fps\":%lu,\"avg_ms\":%.1f,\"max_ms\":%.1f,\"px_per_s\":%lu,\"full_redraw_ms\":%.1f}\n",
             (unsigned long)p.fps, p.avg_ms, p.max_ms, (unsigned long)p.px_per_s,
             full_us < 0 ? -1.0 : full_us / 1000.0);
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_sendstr(req, buf);
}

static void on_close(httpd_handle_t hd, int fd)
{
    if (fd == s_client) {
        s_client = -1;
        ESP_LOGI(TAG, "companion disconnected");
    }
    close(fd);
}

typedef struct {
    int fd;
    size_t len;
    char data[];
} send_job_t;

static void send_work(void *arg)
{
    send_job_t *job = arg;
    if (job->fd == s_client && job->fd >= 0) {
        httpd_ws_frame_t f = {
            .type = HTTPD_WS_TYPE_TEXT,
            .payload = (uint8_t *)job->data,
            .len = job->len,
            .final = true,
        };
        httpd_ws_send_frame_async(s_server, job->fd, &f);
    }
    free(job);
}

void hub_server_send(const char *json)
{
    int fd = s_client;
    if (!s_server || fd < 0) return;
    size_t len = strlen(json);
    send_job_t *job = malloc(sizeof(send_job_t) + len + 1);
    if (!job) return;
    job->fd = fd;
    job->len = len;
    memcpy(job->data, json, len + 1);
    if (httpd_queue_work(s_server, send_work, job) != ESP_OK) {
        free(job);
    }
}

bool hub_server_pc_online(void)
{
    return s_client >= 0 && (uint32_t)(now_ms() - s_last_rx_ms) < ONLINE_TIMEOUT_MS;
}

void hub_server_start(void)
{
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.stack_size = 8192;
    cfg.close_fn = on_close;
    cfg.lru_purge_enable = true;
    cfg.max_open_sockets = 4;
    if (httpd_start(&s_server, &cfg) != ESP_OK) {
        ESP_LOGE(TAG, "http server failed to start");
        return;
    }
    static const httpd_uri_t ws = {
        .uri = "/ws", .method = HTTP_GET, .handler = ws_handler, .is_websocket = true,
    };
    static const httpd_uri_t root = {.uri = "/", .method = HTTP_GET, .handler = root_handler};
    httpd_register_uri_handler(s_server, &ws);
    httpd_register_uri_handler(s_server, &root);
    static const httpd_uri_t shot = {.uri = "/screenshot", .method = HTTP_GET, .handler = screenshot_handler};
    static const httpd_uri_t tap = {.uri = "/tap", .method = HTTP_GET, .handler = tap_handler};
    httpd_register_uri_handler(s_server, &shot);
    httpd_register_uri_handler(s_server, &tap);
    static const httpd_uri_t perf = {.uri = "/perf", .method = HTTP_GET, .handler = perf_handler};
    httpd_register_uri_handler(s_server, &perf);
    ota_register(s_server);
    ESP_LOGI(TAG, "listening on :80");
}

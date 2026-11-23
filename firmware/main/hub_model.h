// Data shown on the hub. The PC companion fills stats, media, events and
// tiles; the hub fetches weather itself.
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <time.h>

typedef struct {
    float cpu;          // %
    float cpu_temp;     // °C, < 0 when unknown
    float ram_used;     // GB
    float ram_total;
    bool has_gpu;
    float gpu;          // %
    float gpu_temp;     // °C, < 0 when unknown
    float vram_used;    // GB
    float vram_total;
    float net_down;     // KB/s
    float net_up;
    float disk;         // % used of the system drive
    char cpu_name[48];
    char gpu_name[48];
} hub_stats_t;

typedef struct {
    bool active;
    bool playing;
    bool can_seek;
    bool no_controls;   // display-only source (a Plex player that can't be remote controlled)
    int pos;            // seconds
    int dur;
    uint32_t art_id;    // 0 = no cover
    char title[128];
    char artist[96];
    char album[96];
    char source[32];
} hub_media_t;

// A Plex or Jellyfin server: its own section on the Media page.
typedef struct {
    bool configured;    // false: no server set up, the Media page shows the PC only
    bool online;
    int streams, transcoding;
    char name[32];
    char kind[12];      // "plex" or "jellyfin"
    char version[48];   // "Plex Media Server 1.41.2"
    char library[96];   // "312 movies, 45 shows, 2,104 episodes"
    hub_media_t now;    // the session shown (now.active false: nothing playing)
} hub_server_t;

#define HUB_MAX_EVENTS 16
typedef struct {
    time_t start;
    time_t end;
    bool all_day;
    uint32_t color;     // 0xRRGGBB
    char title[80];
    char location[64];
    char calendar[32];
} hub_event_t;

#define HUB_MAX_TILES 16
typedef struct {
    uint32_t color;
    char label[32];
    char icon[32];
} hub_tile_t;

#define HUB_CLAUDE_DAYS 7
#define HUB_CLAUDE_MODELS 4
typedef struct {
    bool valid;
    char provider[12];   // "claude" or "codex"
    // Plan limits, from Claude Code's status line (Pro/Max only).
    bool has_5h, has_7d;
    float pct_5h, pct_7d;
    time_t reset_5h, reset_7d;
    time_t limits_updated;          // when Claude Code last reported them
    // Local estimate of the current five-hour window, from transcripts.
    bool block_active;
    time_t block_reset;
    double block_tokens;
    int block_msgs;
    double today_tokens, today_output, last_hour;
    int today_msgs;
    double days[HUB_CLAUDE_DAYS];   // oldest first, last entry is today
    int model_count;
    char model_name[HUB_CLAUDE_MODELS][16];
    double model_tokens[HUB_CLAUDE_MODELS];
} hub_claude_t;

#define HUB_MAX_PRINTERS 4
#define HUB_MAX_AMS 8
typedef struct {
    bool online;
    char name[32];
    char model[16];
    char state[12];       // IDLE, PREPARE, RUNNING, PAUSE, FINISH, FAILED, OFFLINE...
    char file[64];
    char speed[12];
    char error[48];
    int pct;              // 0-100
    int remaining_min;
    int layer, layers;
    float nozzle, nozzle_target, bed, bed_target, chamber;  // NAN when unknown
    int ams_count;
    uint32_t ams_color[HUB_MAX_AMS];   // 0xRRGGBB
    char ams_type[HUB_MAX_AMS][8];
    bool ams_active[HUB_MAX_AMS];
} hub_printer_t;

#define HUB_HOURS 24
#define HUB_DAYS 6
typedef struct {
    bool valid;
    time_t fetched;
    float temp;
    float feels;
    float wind;
    int humidity;
    int code;           // WMO weather code
    bool is_day;
    time_t hour_t[HUB_HOURS];
    float hour_temp[HUB_HOURS];
    int hour_code[HUB_HOURS];
    int hour_precip[HUB_HOURS];
    time_t day_t[HUB_DAYS];
    float day_hi[HUB_DAYS];
    float day_lo[HUB_DAYS];
    int day_code[HUB_DAYS];
    int day_precip[HUB_DAYS];
} hub_weather_t;

// An agent session waiting for the user (full-screen alert).
typedef struct {
    bool active;
    char id[64];        // session id, echoed back in alert_action
    char kind[16];      // "permission" or "input"
    char agent[16];     // "claude"
    char title[64];
    char detail[128];
    char project[48];
} hub_alert_t;

// Steam: the game being played and recently played games.
#define HUB_STEAM_RECENT 5
#define HUB_STEAM_FRIENDS 5
typedef struct {
    uint32_t appid;
    char name[48];
    time_t last;        // last played (recent games)
    int total_min;      // total playtime
} hub_game_t;

typedef struct {
    bool valid;         // a steam message has arrived
    bool found;         // Steam is installed on the PC
    bool playing;
    hub_game_t now;     // when playing
    int session_s;      // seconds into this session when the message was sent
    hub_game_t recent[HUB_STEAM_RECENT];
    int recent_count;
    char friend_name[HUB_STEAM_FRIENDS][24];   // friends in a game (Steam Web API, opt-in)
    char friend_game[HUB_STEAM_FRIENDS][32];
    int friend_count;
} hub_steam_t;

// GitHub contribution chart: a level 0-4 per day for the last year, oldest first.
#define HUB_GH_DAYS 378     // 54 weeks: a year plus the partial weeks at both ends
#define HUB_GH_MONTHS 14
#define HUB_GH_REPOS 4
typedef struct {
    bool valid;
    bool accent;            // shades of the accent colour instead of GitHub's greens
    int total;              // contributions in the last year
    int today, week, last_week, streak, best;
    int start_wday;         // weekday of the first day, 0 = Sunday
    int day_count;          // today is level[day_count - 1]
    uint8_t level[HUB_GH_DAYS];
    int month_count;
    int16_t month_col[HUB_GH_MONTHS];   // week column of each month label
    char month_name[HUB_GH_MONTHS][4];
    // For the GitHub page.
    char login[40];
    int longest;            // longest streak in the year, days
    char best_date[8];      // "Nov 19"
    int week_days[7];       // this week, Sunday to today
    int week_day_count;
    int last_week_days[7];
    int commits, prs, reviews, issues;
    int private_count;      // contributions in private repositories (GitHub won't break these down)
    int repo_count;
    char repo_name[HUB_GH_REPOS][32];   // most commits in the year first
    int repo_commits[HUB_GH_REPOS];
} hub_github_t;

// Internet connection: latency, outages, data used and the last speed test.
#define HUB_NET_HIST 60
typedef struct {
    bool valid;
    bool online;
    float latency_ms;        // < 0: unknown
    float jitter_ms;
    int loss_pct;
    int16_t hist[HUB_NET_HIST];   // latency per probe, oldest first; -1 = lost
    int hist_count;
    time_t up_since;
    time_t drop_at;          // last outage (0 = none seen)
    int drop_dur_s;
    double today_bytes, month_bytes;
    bool has_speed, testing;
    float down_mbps, up_mbps, speed_ping_ms;
    time_t speed_at;
    char ip[48];
} hub_net_t;

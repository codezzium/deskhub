// Media: whatever Windows reports as the current media session
// (Spotify, YouTube in a browser, etc.), with cover art and controls.
// With a Plex or Jellyfin server set up, the page splits: this PC on the left,
// the server on the right (its session, or the server and its library while nothing plays).
#include <ctype.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>

#include "icons.h"
#include "ui_internal.h"

#define ART 280
#define ART_Y 40
#define COL_X (ART + 28)
#define COL_W (CONTENT_W - COL_X)

static lv_obj_t *s_body, *s_empty, *s_empty_sub;
static lv_obj_t *s_art, *s_art_ph;
static lv_obj_t *s_source, *s_title, *s_artist, *s_album;
static lv_obj_t *s_slider, *s_pos, *s_dur, *s_play, *s_controls;
static int s_pos_s;

static void cmd_cb(lv_event_t *e)
{
    ui_send_media((const char *)lv_event_get_user_data(e), 0);
}

static void seek_cb(lv_event_t *e)
{
    lv_event_code_t code = lv_event_get_code(e);
    int v = lv_slider_get_value(s_slider);
    char buf[16];
    if (code == LV_EVENT_VALUE_CHANGED) {
        ui_fmt_duration(buf, sizeof(buf), v);
        lv_label_set_text(s_pos, buf);
    } else if (code == LV_EVENT_RELEASED) {
        s_pos_s = v;
        ui_send_media("seek", v);
    }
}

// ---- split view: this PC on the left, the media server on the right --------
// Shown once the companion says a Plex or Jellyfin server is set up. Each card:
// the cover with the text beside it, the progress bar under, then the buttons.
// The block is centred in the card's height, so a card without buttons (Plex)
// or with nothing playing doesn't leave an empty band at the bottom.

#define CARD_GAP 12
#define TRACK_COL 0x3a3f49   // progress track: visible on a card

typedef struct {
    bool server;
    lv_obj_t *card, *art_ph, *ph_icon, *frame, *art;
    lv_obj_t *source, *title, *artist, *album;
    lv_obj_t *slider, *pos, *dur, *controls, *play;
    int pos_s;
    int inner_w, inner_h, art_w, art_h, tx;
    int layout;   // -1 none yet, else bit 0 progress, bit 1 buttons, bit 2 idle
} media_row_t;

static lv_obj_t *s_split;
static media_row_t s_rows[2];   // 0 = this PC, 1 = the server

static void row_cmd_cb(lv_event_t *e)
{
    media_row_t *r = lv_obj_get_user_data(lv_event_get_current_target(e));
    const char *cmd = lv_event_get_user_data(e);
    if (r->server) ui_send_server(cmd, 0);
    else ui_send_media(cmd, 0);
}

static void row_seek_cb(lv_event_t *e)
{
    media_row_t *r = lv_event_get_user_data(e);
    int v = lv_slider_get_value(r->slider);
    char buf[16];
    if (lv_event_get_code(e) == LV_EVENT_VALUE_CHANGED) {
        ui_fmt_duration(buf, sizeof(buf), v);
        lv_label_set_text(r->pos, buf);
    } else {
        r->pos_s = v;
        if (r->server) ui_send_server("seek", v);
        else ui_send_media("seek", v);
    }
}

static lv_obj_t *row_button(media_row_t *r, int size, uint32_t bg, const char *glyph, const char *cmd)
{
    lv_obj_t *b = ui_round_button(r->controls, size, bg, size > 56 ? g_icon_48 : g_icon_32, glyph, row_cmd_cb,
                                  (void *)cmd);
    lv_obj_set_user_data(b, r);
    return b;
}

static void style_track(lv_obj_t *slider)
{
    lv_obj_set_style_bg_color(slider, lv_color_hex(TRACK_COL), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(slider, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_bg_color(slider, lv_color_hex(COL_TEXT), LV_PART_INDICATOR);
    lv_obj_set_style_bg_opa(slider, LV_OPA_COVER, LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(slider, lv_color_hex(COL_TEXT), LV_PART_KNOB);
    lv_obj_set_style_bg_opa(slider, LV_OPA_COVER, LV_PART_KNOB);
}

static void row_create(media_row_t *r, bool server, int x, int w, int h)
{
    int inner_w = w - 34, inner_h = h - 34;
    int art_h = inner_w * 45 / 100;
    if (art_h > 150) art_h = 150;
    int art_w = server ? art_h * 2 / 3 : art_h;
    int tx = art_w + 14, tw = inner_w - tx;
    r->server = server;
    r->inner_w = inner_w;
    r->inner_h = inner_h;
    r->art_w = art_w;
    r->art_h = art_h;
    r->tx = tx;
    r->layout = -1;
    r->card = ui_card(s_split);
    lv_obj_set_size(r->card, w, h);
    lv_obj_set_pos(r->card, x, 0);
    lv_obj_remove_flag(r->card, LV_OBJ_FLAG_CLICKABLE);

    r->art_ph = lv_obj_create(r->card);
    lv_obj_remove_style_all(r->art_ph);
    lv_obj_set_size(r->art_ph, art_w, art_h);
    lv_obj_set_style_radius(r->art_ph, 12, 0);
    lv_obj_set_style_bg_color(r->art_ph, lv_color_hex(COL_CARD_HI), 0);
    lv_obj_set_style_bg_opa(r->art_ph, LV_OPA_COVER, 0);
    lv_obj_remove_flag(r->art_ph, LV_OBJ_FLAG_CLICKABLE);
    r->ph_icon = ui_label(r->art_ph, g_icon_48, COL_DIM, server ? ICON_MOVIE : ICON_MUSIC_NOTE);
    lv_obj_center(r->ph_icon);

    r->frame = lv_obj_create(r->card);
    lv_obj_remove_style_all(r->frame);
    lv_obj_set_size(r->frame, art_w, art_h);
    lv_obj_set_style_radius(r->frame, 12, 0);
    lv_obj_set_style_clip_corner(r->frame, true, 0);
    lv_obj_remove_flag(r->frame, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    r->art = lv_image_create(r->frame);
    lv_obj_set_size(r->art, art_w, art_h);
    lv_image_set_inner_align(r->art, LV_IMAGE_ALIGN_STRETCH);
    lv_obj_add_flag(r->art, LV_OBJ_FLAG_HIDDEN);

    r->source = ui_label(r->card, g_font_14, COL_ACCENT, "");
    lv_obj_set_width(r->source, tw);
    lv_label_set_long_mode(r->source, LV_LABEL_LONG_DOT);
    r->title = ui_label(r->card, g_font_24, COL_TEXT, "");
    ui_marquee(r->title);
    lv_obj_set_width(r->title, tw);
    r->artist = ui_label(r->card, g_font_16, COL_TEXT, "");
    ui_marquee(r->artist);
    lv_obj_set_width(r->artist, tw);
    r->album = ui_label(r->card, g_font_14, COL_MUTED, "");
    ui_marquee(r->album);
    lv_obj_set_width(r->album, tw);

    r->slider = lv_slider_create(r->card);
    lv_obj_set_size(r->slider, inner_w - 12, 6);
    lv_slider_set_range(r->slider, 0, 1);
    style_track(r->slider);
    lv_obj_set_style_pad_all(r->slider, 5, LV_PART_KNOB);
    lv_obj_set_ext_click_area(r->slider, 16);
    lv_obj_add_event_cb(r->slider, row_seek_cb, LV_EVENT_VALUE_CHANGED, r);
    lv_obj_add_event_cb(r->slider, row_seek_cb, LV_EVENT_RELEASED, r);
    r->pos = ui_label(r->card, g_font_14, COL_MUTED, "0:00");
    r->dur = ui_label(r->card, g_font_14, COL_MUTED, "");
    lv_obj_set_width(r->dur, 80);
    lv_obj_set_style_text_align(r->dur, LV_TEXT_ALIGN_RIGHT, 0);

    r->controls = lv_obj_create(r->card);
    lv_obj_remove_style_all(r->controls);
    lv_obj_set_size(r->controls, inner_w, 68);
    lv_obj_set_flex_flow(r->controls, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(r->controls, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(r->controls, 20, 0);
    lv_obj_remove_flag(r->controls, LV_OBJ_FLAG_SCROLLABLE);
    row_button(r, 52, COL_CARD_HI, ICON_SKIP_PREVIOUS, "prev");
    r->play = row_button(r, 68, COL_TEXT, ICON_PLAY_ARROW, "toggle");
    lv_obj_set_style_text_color(lv_obj_get_child(r->play, 0), lv_color_hex(COL_BG), 0);
    row_button(r, 52, COL_CARD_HI, ICON_SKIP_NEXT, "next");

    // The rest is placed by row_layout().
    lv_obj_set_x(r->slider, 6);
    lv_obj_set_x(r->pos, 0);
    lv_obj_set_x(r->dur, inner_w - 80);
    lv_obj_set_x(r->controls, 0);
}

// Place everything for what the card shows now, centred in its height.
static void row_layout(media_row_t *r, bool progress, bool buttons, bool idle)
{
    int layout = (progress ? 1 : 0) | (buttons ? 2 : 0) | (idle ? 4 : 0);
    if (layout == r->layout) return;
    r->layout = layout;
    int a = r->art_h;
    lv_obj_t *text[4] = {r->source, r->title, r->artist, r->album};
    if (idle) {
        // Nothing playing: the placeholder centred on top, the lines centred under it.
        int y = (r->inner_h - (a + 16 + 100)) / 2;
        if (y < 0) y = 0;
        int ax = (r->inner_w - r->art_w) / 2;
        lv_obj_set_pos(r->art_ph, ax, y);
        lv_obj_set_pos(r->frame, ax, y);
        static const int dy[4] = {0, 20, 54, 80};
        for (int i = 0; i < 4; i++) {
            lv_obj_set_width(text[i], r->inner_w);
            lv_obj_set_style_text_align(text[i], LV_TEXT_ALIGN_CENTER, 0);
            lv_obj_set_pos(text[i], 0, y + a + 16 + dy[i]);
        }
        return;
    }
    for (int i = 0; i < 4; i++) {
        lv_obj_set_width(text[i], r->inner_w - r->tx);
        lv_obj_set_style_text_align(text[i], LV_TEXT_ALIGN_LEFT, 0);
        lv_obj_set_x(text[i], r->tx);
    }
    lv_obj_set_x(r->art_ph, 0);
    lv_obj_set_x(r->frame, 0);
    int block = a + (progress ? 56 : 0) + (buttons ? 84 : 0);
    int y = (r->inner_h - block) / 2;
    if (y < 0) y = 0;
    lv_obj_set_y(r->art_ph, y);
    lv_obj_set_y(r->frame, y);
    // Text beside the cover, centred on it.
    int ty = y + (a - 100) / 2;
    lv_obj_set_y(r->source, ty);
    lv_obj_set_y(r->title, ty + 20);
    lv_obj_set_y(r->artist, ty + 54);
    lv_obj_set_y(r->album, ty + 80);
    int py = y + a + 24;
    lv_obj_set_y(r->slider, py);
    lv_obj_set_y(r->pos, py + 14);
    lv_obj_set_y(r->dur, py + 14);
    lv_obj_set_y(r->controls, y + a + (progress ? 56 : 0) + 16);
}

static void show(lv_obj_t *o, bool on)
{
    if (on) lv_obj_remove_flag(o, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_add_flag(o, LV_OBJ_FLAG_HIDDEN);
}

static void row_show_pos(media_row_t *r)
{
    char buf[16];
    if (lv_slider_is_dragged(r->slider)) return;
    lv_slider_set_value(r->slider, r->pos_s, LV_ANIM_OFF);
    ui_fmt_duration(buf, sizeof(buf), r->pos_s);
    lv_label_set_text(r->pos, buf);
}

static void set_text(lv_obj_t *label, const char *text)
{
    // Leave an unchanged label alone so its scrolling doesn't restart every second.
    if (strcmp(lv_label_get_text(label), text) != 0) lv_label_set_text(label, text);
}

// Fill a card with a session, or with `idle_*` lines when nothing plays.
static void row_fill(media_row_t *r, const hub_media_t *m, const char *source,
                     const char *idle_title, const char *idle_sub, const char *idle_small)
{
    char src[64];
    strlcpy(src, source, sizeof(src));
    for (char *p = src; *p; p++) *p = toupper((unsigned char)*p);
    set_text(r->source, src);
    bool on = m->active;
    set_text(r->title, on ? m->title : idle_title);
    set_text(r->artist, on ? m->artist : idle_sub);
    set_text(r->album, on ? m->album : idle_small);
    lv_obj_set_style_text_color(r->title, lv_color_hex(on ? COL_TEXT : COL_MUTED), 0);
    lv_obj_set_style_text_color(r->artist, lv_color_hex(on ? COL_TEXT : COL_MUTED), 0);
    bool progress = on && m->dur > 0, buttons = on && !m->no_controls;
    show(r->slider, progress);
    show(r->pos, progress);
    show(r->dur, progress);
    show(r->controls, buttons);
    row_layout(r, progress, buttons, !on);
    if (!on) return;
    lv_label_set_text(lv_obj_get_child(r->play, 0), m->playing ? ICON_PAUSE : ICON_PLAY_ARROW);
    char buf[16];
    lv_slider_set_range(r->slider, 0, m->dur > 0 ? m->dur : 1);
    ui_fmt_duration(buf, sizeof(buf), m->dur);
    lv_label_set_text(r->dur, buf);
    if (m->can_seek && m->dur > 0) lv_obj_add_flag(r->slider, LV_OBJ_FLAG_CLICKABLE);
    else lv_obj_remove_flag(r->slider, LV_OBJ_FLAG_CLICKABLE);
    r->pos_s = m->pos;
    row_show_pos(r);
}

static void row_art(media_row_t *r, bool has, const lv_image_dsc_t *img)
{
    if (has) {
        lv_image_set_src(r->art, img);
        lv_obj_invalidate(r->art);
    }
    show(r->art, has);
    show(r->art_ph, !has);
}

static void split_create(lv_obj_t *page)
{
    s_split = lv_obj_create(page);
    lv_obj_remove_style_all(s_split);
    lv_obj_set_size(s_split, LV_PCT(100), LV_PCT(100));
    lv_obj_remove_flag(s_split, LV_OBJ_FLAG_SCROLLABLE);
    int w = (CONTENT_W - CARD_GAP) / 2;
    row_create(&s_rows[0], false, 0, w, CONTENT_H);
    row_create(&s_rows[1], true, w + CARD_GAP, CONTENT_W - w - CARD_GAP, CONTENT_H);
    lv_obj_add_flag(s_split, LV_OBJ_FLAG_HIDDEN);
}

static void split_refresh_pc(void)
{
    row_fill(&s_rows[0], &g_media, g_media.active ? g_media.source : "This PC",
             g_pc_online ? "Nothing playing on this PC" : "PC offline",
             g_pc_online ? "" : "Start the companion app on your PC", "");
}

void media_refresh_server(void)
{
    if (!s_split) return;
    if (!g_server.configured) {
        media_refresh();   // back to the PC-only page
        return;
    }
    const hub_server_t *s = &g_server;
    char src[64];
    const char *kind = strcmp(s->kind, "plex") == 0 ? "Plex" : strcmp(s->kind, "jellyfin") == 0 ? "Jellyfin" : "";
    // "BEANS - PLEX" (just "PLEX" when the server kept its default name).
    bool both = kind[0] && strcasecmp(kind, s->name) != 0;
    snprintf(src, sizeof(src), "%.31s%s%s%s", s->name, both ? " - " : "", both ? kind : "",
             s->now.active && s->transcoding ? " - transcoding" : "");
    char idle[64];
    if (s->online) strlcpy(idle, "Nothing playing right now", sizeof(idle));
    else snprintf(idle, sizeof(idle), "Can't reach %.31s", s->name[0] ? s->name : "the server");
    row_fill(&s_rows[1], &s->now, src, idle, s->online ? s->library : "", s->version);
    if (lv_obj_has_flag(s_split, LV_OBJ_FLAG_HIDDEN)) media_refresh();
}

void media_refresh_server_art(void)
{
    if (s_split) row_art(&s_rows[1], ui_server_has_art(), &g_srv_art);
    home_refresh_media();
}

void media_create(lv_obj_t *page)
{
    s_body = lv_obj_create(page);
    lv_obj_remove_style_all(s_body);
    lv_obj_set_size(s_body, LV_PCT(100), LV_PCT(100));
    lv_obj_remove_flag(s_body, LV_OBJ_FLAG_SCROLLABLE);

    s_art_ph = lv_obj_create(s_body);
    lv_obj_remove_style_all(s_art_ph);
    lv_obj_set_size(s_art_ph, ART, ART);
    lv_obj_set_pos(s_art_ph, 0, ART_Y);
    lv_obj_set_style_radius(s_art_ph, 20, 0);
    lv_obj_set_style_bg_color(s_art_ph, lv_color_hex(COL_CARD), 0);
    lv_obj_set_style_bg_opa(s_art_ph, LV_OPA_COVER, 0);
    lv_obj_center(ui_label(s_art_ph, g_icon_80, COL_DIM, ICON_MUSIC_NOTE));

    // Rounded frame that clips the cover (images ignore clip_corner themselves).
    lv_obj_t *frame = lv_obj_create(s_body);
    lv_obj_remove_style_all(frame);
    lv_obj_set_size(frame, ART, ART);
    lv_obj_set_pos(frame, 0, ART_Y);
    lv_obj_set_style_radius(frame, 22, 0);
    lv_obj_set_style_clip_corner(frame, true, 0);
    lv_obj_remove_flag(frame, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    s_art = lv_image_create(frame);
    lv_obj_set_size(s_art, ART, ART);
    lv_image_set_inner_align(s_art, LV_IMAGE_ALIGN_STRETCH);
    lv_obj_add_flag(s_art, LV_OBJ_FLAG_HIDDEN);

    s_source = ui_label(s_body, g_font_14, COL_ACCENT, "");
    lv_obj_set_pos(s_source, COL_X, 52);
    s_title = ui_label(s_body, g_font_32, COL_TEXT, "");
    ui_marquee(s_title);
    lv_obj_set_width(s_title, COL_W);
    lv_obj_set_pos(s_title, COL_X, 76);
    s_artist = ui_label(s_body, g_font_24, COL_TEXT, "");
    ui_marquee(s_artist);
    lv_obj_set_width(s_artist, COL_W);
    lv_obj_set_pos(s_artist, COL_X, 122);
    s_album = ui_label(s_body, g_font_16, COL_MUTED, "");
    ui_marquee(s_album);
    lv_obj_set_width(s_album, COL_W);
    lv_obj_set_pos(s_album, COL_X, 156);

    s_slider = lv_slider_create(s_body);
    lv_obj_set_size(s_slider, COL_W - 16, 8);
    lv_obj_set_pos(s_slider, COL_X + 8, 214);
    lv_slider_set_range(s_slider, 0, 1);
    style_track(s_slider);
    lv_obj_set_style_pad_all(s_slider, 6, LV_PART_KNOB);
    lv_obj_set_ext_click_area(s_slider, 20);
    lv_obj_add_event_cb(s_slider, seek_cb, LV_EVENT_VALUE_CHANGED, NULL);
    lv_obj_add_event_cb(s_slider, seek_cb, LV_EVENT_RELEASED, NULL);

    s_pos = ui_label(s_body, g_font_14, COL_MUTED, "0:00");
    lv_obj_set_pos(s_pos, COL_X, 234);
    s_dur = ui_label(s_body, g_font_14, COL_MUTED, "0:00");
    lv_obj_set_width(s_dur, 80);
    lv_obj_set_style_text_align(s_dur, LV_TEXT_ALIGN_RIGHT, 0);
    lv_obj_set_pos(s_dur, COL_X + COL_W - 80, 234);

    lv_obj_t *row = s_controls = lv_obj_create(s_body);
    lv_obj_remove_style_all(row);
    lv_obj_set_size(row, COL_W, 96);
    lv_obj_set_pos(row, COL_X, 270);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(row, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(row, 28, 0);
    lv_obj_remove_flag(row, LV_OBJ_FLAG_SCROLLABLE);
    ui_round_button(row, 68, COL_CARD, g_icon_48, ICON_SKIP_PREVIOUS, cmd_cb, "prev");
    s_play = ui_round_button(row, 92, COL_TEXT, g_icon_48, ICON_PLAY_ARROW, cmd_cb, "toggle");
    lv_obj_set_style_text_color(lv_obj_get_child(s_play, 0), lv_color_hex(COL_BG), 0);
    ui_round_button(row, 68, COL_CARD, g_icon_48, ICON_SKIP_NEXT, cmd_cb, "next");

    s_empty = lv_obj_create(page);
    lv_obj_remove_style_all(s_empty);
    lv_obj_set_size(s_empty, LV_PCT(100), LV_PCT(100));
    lv_obj_t *icon = ui_label(s_empty, g_icon_80, COL_DIM, ICON_MUSIC_NOTE);
    lv_obj_align(icon, LV_ALIGN_CENTER, 0, -40);
    lv_obj_t *msg = ui_label(s_empty, g_font_24, COL_MUTED, "Nothing playing");
    lv_obj_align(msg, LV_ALIGN_CENTER, 0, 24);
    s_empty_sub = ui_label(s_empty, g_font_16, COL_DIM, "");
    lv_obj_align(s_empty_sub, LV_ALIGN_CENTER, 0, 56);

    split_create(page);
    media_refresh();
}

static void show_pos(void)
{
    char buf[16];
    if (lv_slider_is_dragged(s_slider)) return;
    lv_slider_set_value(s_slider, s_pos_s, LV_ANIM_OFF);
    ui_fmt_duration(buf, sizeof(buf), s_pos_s);
    lv_label_set_text(s_pos, buf);
}

void media_refresh(void)
{
    const hub_media_t *m = &g_media;
    if (g_server.configured) {
        lv_obj_add_flag(s_body, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s_empty, LV_OBJ_FLAG_HIDDEN);
        if (lv_obj_has_flag(s_split, LV_OBJ_FLAG_HIDDEN)) {
            lv_obj_remove_flag(s_split, LV_OBJ_FLAG_HIDDEN);
            media_refresh_server();
            row_art(&s_rows[0], ui_media_has_art(), &g_art);
            row_art(&s_rows[1], ui_server_has_art(), &g_srv_art);
        }
        split_refresh_pc();
        return;
    }
    lv_obj_add_flag(s_split, LV_OBJ_FLAG_HIDDEN);
    if (!m->active) {
        lv_obj_add_flag(s_body, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(s_empty, LV_OBJ_FLAG_HIDDEN);
        lv_label_set_text(s_empty_sub, g_pc_online ? "Play something on your PC"
                                                   : "Start the companion app on your PC");
        return;
    }
    lv_obj_remove_flag(s_body, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(s_empty, LV_OBJ_FLAG_HIDDEN);

    char src[32];
    strlcpy(src, m->source, sizeof(src));
    for (char *p = src; *p; p++) *p = toupper((unsigned char)*p);
    lv_label_set_text(s_source, src);
    if (strcmp(lv_label_get_text(s_title), m->title) != 0) lv_label_set_text(s_title, m->title);
    lv_label_set_text(s_artist, m->artist);
    lv_label_set_text(s_album, m->album);
    lv_label_set_text(lv_obj_get_child(s_play, 0), m->playing ? ICON_PAUSE : ICON_PLAY_ARROW);
    if (m->no_controls) lv_obj_add_flag(s_controls, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_remove_flag(s_controls, LV_OBJ_FLAG_HIDDEN);

    char buf[16];
    int dur = m->dur > 0 ? m->dur : 1;
    lv_slider_set_range(s_slider, 0, dur);
    ui_fmt_duration(buf, sizeof(buf), m->dur);
    lv_label_set_text(s_dur, m->dur > 0 ? buf : "");
    if (m->can_seek && m->dur > 0) lv_obj_add_flag(s_slider, LV_OBJ_FLAG_CLICKABLE);
    else lv_obj_remove_flag(s_slider, LV_OBJ_FLAG_CLICKABLE);
    s_pos_s = m->pos;
    show_pos();
}

void media_refresh_art(void)
{
    row_art(&s_rows[0], ui_media_has_art(), &g_art);
    if (ui_media_has_art()) {
        lv_image_set_src(s_art, &g_art);
        lv_obj_invalidate(s_art);
        lv_obj_remove_flag(s_art, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s_art_ph, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(s_art, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(s_art_ph, LV_OBJ_FLAG_HIDDEN);
    }
    home_refresh_media();
}

void media_tick(void)
{
    // Advance locally between the companion's once-a-second updates.
    if (g_media.active && g_media.playing && g_media.dur > 0 && s_pos_s < g_media.dur) {
        s_pos_s++;
        show_pos();
    }
    const hub_media_t *ms[2] = {&g_media, &g_server.now};
    for (int i = 0; i < 2 && s_split; i++) {
        media_row_t *r = &s_rows[i];
        if (ms[i]->active && ms[i]->playing && ms[i]->dur > 0 && r->pos_s < ms[i]->dur) {
            r->pos_s++;
            row_show_pos(r);
        }
    }
}

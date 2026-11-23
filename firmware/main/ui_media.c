// Media: whatever Windows reports as the current media session
// (Spotify, YouTube in a browser, etc.), with cover art and controls.
// With nothing playing on the PC, the companion may send a Plex or Jellyfin
// session instead (display-only when "controls" is false).
#include <ctype.h>
#include <stdio.h>
#include <string.h>

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
    lv_obj_set_style_bg_color(s_slider, lv_color_hex(COL_CARD_HI), LV_PART_MAIN);
    lv_obj_set_style_bg_color(s_slider, lv_color_hex(COL_TEXT), LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(s_slider, lv_color_hex(COL_TEXT), LV_PART_KNOB);
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
}

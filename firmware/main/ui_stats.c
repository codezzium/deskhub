// PC stats: four gauges and a one-minute CPU/GPU history.
#include <stdio.h>
#include <string.h>

#include "icons.h"
#include "ui_internal.h"

#define GAUGES 4
#define G_CPU 0
#define G_RAM 1
#define G_GPU 2
#define G_GPU2 3
#define ARC_D 112
#define RING_D 86
#define GAUGE_W ((CONTENT_W - 3 * 12) / 4)
#define HISTORY 60
#define SWITCH_H 40                       // PC / Internet switcher
#define VIEW_Y (SWITCH_H + 12)
#define VIEW_H (CONTENT_H - VIEW_Y)        // 388
#define GAUGE_H 176

static lv_obj_t *s_arc[GAUGES], *s_ring[GAUGES], *s_val[GAUGES], *s_sub[GAUGES], *s_name[GAUGES];
static lv_obj_t *s_chart, *s_net, *s_disk, *s_offline, *s_names, *s_gpu_key, *s_gpu2_key;
static lv_chart_series_t *s_cpu_ser, *s_gpu_ser, *s_gpu2_ser;

static const char *NAMES[GAUGES] = {"CPU", "RAM", "GPU", "VRAM"};
static const uint32_t COLORS[GAUGES] = {COL_ACCENT2, COL_DATA3, COL_CYAN, 0xf79ad3};

static lv_obj_t *make_arc(lv_obj_t *parent, int d, int w, uint32_t color)
{
    lv_obj_t *arc = lv_arc_create(parent);
    lv_obj_set_size(arc, d, d);
    lv_obj_align(arc, LV_ALIGN_TOP_MID, 0, (ARC_D - d) / 2);
    lv_arc_set_rotation(arc, 135);
    lv_arc_set_bg_angles(arc, 0, 270);
    lv_arc_set_range(arc, 0, 100);
    lv_arc_set_value(arc, 0);
    lv_obj_remove_style(arc, NULL, LV_PART_KNOB);
    lv_obj_remove_flag(arc, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_style_arc_width(arc, w, LV_PART_MAIN);
    lv_obj_set_style_arc_width(arc, w, LV_PART_INDICATOR);
    lv_obj_set_style_arc_rounded(arc, true, LV_PART_INDICATOR);
    lv_obj_set_style_arc_color(arc, lv_color_hex(COL_CARD_HI), LV_PART_MAIN);
    lv_obj_set_style_arc_color(arc, lv_color_hex(color), LV_PART_INDICATOR);
    return arc;
}

static void make_gauge(lv_obj_t *page, int i)
{
    lv_obj_t *c = ui_card(page);
    lv_obj_set_size(c, GAUGE_W, GAUGE_H);
    lv_obj_set_pos(c, i * (GAUGE_W + 12), 0);

    s_arc[i] = make_arc(c, ARC_D, 12, COLORS[i]);
    if (i >= G_GPU) {
        s_ring[i] = make_arc(c, RING_D, 7, COLORS[i]);
        lv_obj_set_style_arc_opa(s_ring[i], LV_OPA_60, LV_PART_INDICATOR);
        lv_obj_add_flag(s_ring[i], LV_OBJ_FLAG_HIDDEN);
    }

    s_val[i] = ui_label(c, g_font_32, COL_TEXT, "--");
    lv_obj_set_width(s_val[i], ARC_D);
    lv_obj_set_style_text_align(s_val[i], LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_align_to(s_val[i], s_arc[i], LV_ALIGN_CENTER, 0, -4);
    s_name[i] = ui_label(c, g_font_20, COL_TEXT, NAMES[i]);
    lv_obj_align(s_name[i], LV_ALIGN_BOTTOM_MID, 0, -22);
    s_sub[i] = ui_label(c, g_font_14, COL_MUTED, "");
    lv_obj_align(s_sub[i], LV_ALIGN_BOTTOM_MID, 0, 0);
}

static lv_obj_t *s_views[2], *s_switch, *s_seg[2];
static int s_view;

// Show the PC view (0) or the Internet view (1).
void stats_set_view(int v)
{
    if (!s_views[0]) return;
    s_view = v;
    for (int i = 0; i < 2; i++) {
        if (i == v) lv_obj_remove_flag(s_views[i], LV_OBJ_FLAG_HIDDEN);
        else lv_obj_add_flag(s_views[i], LV_OBJ_FLAG_HIDDEN);
        lv_obj_set_style_bg_opa(s_seg[i], i == v ? LV_OPA_COVER : LV_OPA_TRANSP, 0);
        lv_obj_set_style_text_color(lv_obj_get_child(s_seg[i], 0), lv_color_hex(i == v ? COL_BG : COL_MUTED), 0);
    }
}

// The Internet view exists only while the "network" page is turned on.
void stats_set_net_enabled(bool on)
{
    if (!s_switch) return;
    if (on) lv_obj_remove_flag(s_switch, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_add_flag(s_switch, LV_OBJ_FLAG_HIDDEN);
    if (!on && s_view) stats_set_view(0);
}

static void seg_cb(lv_event_t *e)
{
    stats_set_view((int)(intptr_t)lv_event_get_user_data(e));
}

static lv_obj_t *make_view(lv_obj_t *page)
{
    lv_obj_t *v = lv_obj_create(page);
    lv_obj_remove_style_all(v);
    lv_obj_set_size(v, CONTENT_W, VIEW_H);
    lv_obj_set_pos(v, 0, VIEW_Y);
    lv_obj_remove_flag(v, LV_OBJ_FLAG_SCROLLABLE);
    return v;
}

void stats_create(lv_obj_t *page)
{
    // Segmented switcher: PC | Internet.
    s_switch = lv_obj_create(page);
    lv_obj_remove_style_all(s_switch);
    lv_obj_set_size(s_switch, 2 * 128 + 8, SWITCH_H);
    lv_obj_set_pos(s_switch, 0, 0);
    lv_obj_set_style_radius(s_switch, SWITCH_H / 2, 0);
    lv_obj_set_style_bg_color(s_switch, lv_color_hex(COL_CARD), 0);
    lv_obj_set_style_bg_opa(s_switch, LV_OPA_COVER, 0);
    lv_obj_set_style_border_color(s_switch, lv_color_hex(COL_BORDER), 0);
    lv_obj_set_style_border_width(s_switch, 1, 0);
    lv_obj_remove_flag(s_switch, LV_OBJ_FLAG_SCROLLABLE);
    static const char *labels[2] = {"PC", "Internet"};
    for (int i = 0; i < 2; i++) {
        lv_obj_t *b = lv_button_create(s_switch);
        lv_obj_remove_style_all(b);
        lv_obj_set_size(b, 128, SWITCH_H - 8);
        lv_obj_set_pos(b, 4 + i * 128, 3);
        lv_obj_set_style_radius(b, (SWITCH_H - 8) / 2, 0);
        lv_obj_set_style_bg_color(b, lv_color_hex(COL_ACCENT), 0);
        lv_obj_set_ext_click_area(b, 6);
        lv_obj_t *l = ui_label(b, g_font_16, COL_MUTED, labels[i]);
        lv_obj_center(l);
        lv_obj_add_event_cb(b, seg_cb, LV_EVENT_CLICKED, (void *)(intptr_t)i);
        s_seg[i] = b;
    }
    s_views[0] = make_view(page);
    s_views[1] = make_view(page);
    net_page_create(s_views[1]);
    page = s_views[0];

    for (int i = 0; i < GAUGES; i++) make_gauge(page, i);

    lv_obj_t *c = ui_card(page);
    lv_obj_set_size(c, CONTENT_W, VIEW_H - GAUGE_H - 12);
    lv_obj_set_pos(c, 0, GAUGE_H + 12);

    lv_obj_t *t = ui_caption(c, "LAST MINUTE");
    lv_obj_set_pos(t, 0, 0);
    lv_obj_t *cpu_key = ui_label(c, g_font_14, COL_ACCENT2, "\xE2\x80\xA2 CPU");
    lv_obj_set_pos(cpu_key, 150, 0);
    s_gpu_key = ui_label(c, g_font_14, COL_CYAN, "\xE2\x80\xA2 GPU");
    lv_obj_set_pos(s_gpu_key, 210, 0);
    s_gpu2_key = ui_label(c, g_font_14, COLORS[G_GPU2], "");
    lv_obj_align_to(s_gpu2_key, s_gpu_key, LV_ALIGN_OUT_RIGHT_MID, 16, 0);

    s_net = ui_label(c, g_font_16, COL_TEXT, "");
    lv_obj_align(s_net, LV_ALIGN_TOP_RIGHT, 0, -2);
    s_disk = ui_label(c, g_font_14, COL_MUTED, "");
    lv_obj_align(s_disk, LV_ALIGN_BOTTOM_LEFT, 0, -16);
    s_names = ui_label(c, g_font_14, COL_DIM, "");
    lv_obj_align(s_names, LV_ALIGN_BOTTOM_LEFT, 0, 4);

    s_chart = lv_chart_create(c);
    lv_obj_set_size(s_chart, CONTENT_W - 32, VIEW_H - GAUGE_H - 12 - 32 - 28 - 42);
    lv_obj_set_pos(s_chart, 0, 28);
    lv_chart_set_type(s_chart, LV_CHART_TYPE_LINE);
    lv_chart_set_point_count(s_chart, HISTORY);
    lv_chart_set_range(s_chart, LV_CHART_AXIS_PRIMARY_Y, 0, 100);
    lv_chart_set_div_line_count(s_chart, 4, 0);
    lv_chart_set_update_mode(s_chart, LV_CHART_UPDATE_MODE_SHIFT);
    lv_obj_set_style_bg_opa(s_chart, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(s_chart, 0, 0);
    lv_obj_set_style_pad_all(s_chart, 0, 0);
    lv_obj_set_style_line_color(s_chart, lv_color_hex(COL_CARD_HI), LV_PART_MAIN);
    lv_obj_set_style_size(s_chart, 0, 0, LV_PART_INDICATOR);
    lv_obj_set_style_line_width(s_chart, 3, LV_PART_ITEMS);
    s_cpu_ser = lv_chart_add_series(s_chart, lv_color_hex(COL_ACCENT2), LV_CHART_AXIS_PRIMARY_Y);
    s_gpu_ser = lv_chart_add_series(s_chart, lv_color_hex(COL_CYAN), LV_CHART_AXIS_PRIMARY_Y);
    s_gpu2_ser = lv_chart_add_series(s_chart, lv_color_hex(COLORS[G_GPU2]), LV_CHART_AXIS_PRIMARY_Y);
    lv_chart_set_all_value(s_chart, s_cpu_ser, LV_CHART_POINT_NONE);
    lv_chart_set_all_value(s_chart, s_gpu_ser, LV_CHART_POINT_NONE);
    lv_chart_set_all_value(s_chart, s_gpu2_ser, LV_CHART_POINT_NONE);

    s_offline = ui_label(c, g_font_20, COL_DIM, "Start the companion app on your PC");
    lv_obj_center(s_offline);
    stats_set_view(0);
}

static void fmt_rate(char *buf, size_t n, float kbps)
{
    if (kbps >= 1024) snprintf(buf, n, "%.1f MB/s", kbps / 1024);
    else snprintf(buf, n, "%d KB/s", (int)kbps);
}

static void set_gauge(int i, float pct, const char *sub)
{
    char buf[12];
    lv_arc_set_value(s_arc[i], (int)pct);
    snprintf(buf, sizeof(buf), "%d%%", (int)(pct + 0.5f));
    lv_label_set_text(s_val[i], buf);
    lv_label_set_text(s_sub[i], sub);
}

static void clear_gauge(int i, const char *sub)
{
    lv_arc_set_value(s_arc[i], 0);
    lv_label_set_text(s_val[i], "--");
    lv_label_set_text(s_sub[i], sub);
}

static void set_ring(int i, float used, float total)
{
    if (total > 0) {
        lv_arc_set_value(s_ring[i], (int)(100.0f * used / total));
        lv_obj_remove_flag(s_ring[i], LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(s_ring[i], LV_OBJ_FLAG_HIDDEN);
    }
}

static const char *short_gpu(const char *name, const char *fallback)
{
    if (!name[0]) return fallback;
    if (strncmp(name, "GeForce ", 8) == 0) return name + 8;
    return name;
}

static void set_gpu(int i, float load, float temp, float used, float total, const char *name)
{
    char sub[32], t[12] = "";
    if (temp >= 0) snprintf(t, sizeof(t), "%d\xC2\xB0" "C  ", (int)temp);
    if (total > 0) snprintf(sub, sizeof(sub), "%s%.1f/%.0f GB", t, used, total);
    else snprintf(sub, sizeof(sub), "%s", t);
    set_gauge(i, load, sub);
    set_ring(i, used, total);
    lv_label_set_text(s_name[i], short_gpu(name, NAMES[i]));
}

void stats_refresh(void)
{
    if (!g_pc_online) {
        for (int i = 0; i < GAUGES; i++) clear_gauge(i, "");
        for (int i = G_GPU; i < GAUGES; i++) lv_obj_add_flag(s_ring[i], LV_OBJ_FLAG_HIDDEN);
        lv_label_set_text(s_net, "");
        lv_label_set_text(s_disk, "");
        lv_label_set_text(s_names, "");
        lv_obj_remove_flag(s_offline, LV_OBJ_FLAG_HIDDEN);
        return;
    }
    lv_obj_add_flag(s_offline, LV_OBJ_FLAG_HIDDEN);
    const hub_stats_t *s = &g_stats;
    char sub[32];

    if (s->cpu_temp >= 0) snprintf(sub, sizeof(sub), "%d\xC2\xB0" "C", (int)s->cpu_temp);
    else sub[0] = 0;
    set_gauge(G_CPU, s->cpu, sub);

    if (s->has_gpu) {
        set_gpu(G_GPU, s->gpu, s->gpu_temp, s->vram_used, s->vram_total, s->gpu_name);
    } else {
        clear_gauge(G_GPU, "No GPU data");
        lv_obj_add_flag(s_ring[G_GPU], LV_OBJ_FLAG_HIDDEN);
    }

    if (s->has_gpu2) {
        set_gpu(G_GPU2, s->gpu2, s->gpu2_temp, s->vram2_used, s->vram2_total, s->gpu2_name);
    } else {
        lv_obj_add_flag(s_ring[G_GPU2], LV_OBJ_FLAG_HIDDEN);
        lv_label_set_text(s_name[G_GPU2], NAMES[G_GPU2]);
        if (s->has_gpu && s->vram_total > 0) {
            snprintf(sub, sizeof(sub), "%.1f / %.0f GB", s->vram_used, s->vram_total);
            set_gauge(G_GPU2, 100.0f * s->vram_used / s->vram_total, sub);
        } else {
            clear_gauge(G_GPU2, "");
        }
    }

    if (s->ram_total > 0) {
        snprintf(sub, sizeof(sub), "%.1f / %.0f GB", s->ram_used, s->ram_total);
        set_gauge(G_RAM, 100.0f * s->ram_used / s->ram_total, sub);
    }

    char key[64];
    snprintf(key, sizeof(key), "\xE2\x80\xA2 %.47s", short_gpu(s->gpu_name, "GPU"));
    lv_label_set_text(s_gpu_key, key);
    if (s->has_gpu2) snprintf(key, sizeof(key), "\xE2\x80\xA2 %.47s", short_gpu(s->gpu2_name, "GPU 2"));
    else key[0] = 0;
    lv_label_set_text(s_gpu2_key, key);
    lv_obj_align_to(s_gpu2_key, s_gpu_key, LV_ALIGN_OUT_RIGHT_MID, 16, 0);

    char down[20], up[20], buf[160];
    fmt_rate(down, sizeof(down), s->net_down);
    fmt_rate(up, sizeof(up), s->net_up);
    snprintf(buf, sizeof(buf), "↓ %s    ↑ %s", down, up);
    lv_label_set_text(s_net, buf);
    char rd[20], wr[20], temp[16] = "";
    fmt_rate(rd, sizeof(rd), s->disk_read);
    fmt_rate(wr, sizeof(wr), s->disk_write);
    if (s->disk_temp >= 0) snprintf(temp, sizeof(temp), "  %d\xC2\xB0" "C", (int)s->disk_temp);
    snprintf(buf, sizeof(buf), "Disk R %s  W %s%s  %d%%", rd, wr, temp, (int)s->disk);
    lv_label_set_text(s_disk, buf);
    snprintf(buf, sizeof(buf), "%.47s%s%.47s%s%.47s", s->cpu_name, s->gpu_name[0] ? "  /  " : "", s->gpu_name,
             s->gpu2_name[0] ? "  /  " : "", s->gpu2_name);
    lv_label_set_text(s_names, buf);

    lv_chart_set_next_value(s_chart, s_cpu_ser, (int32_t)s->cpu);
    lv_chart_set_next_value(s_chart, s_gpu_ser, s->has_gpu ? (int32_t)s->gpu : LV_CHART_POINT_NONE);
    lv_chart_set_next_value(s_chart, s_gpu2_ser, s->has_gpu2 ? (int32_t)s->gpu2 : LV_CHART_POINT_NONE);
}

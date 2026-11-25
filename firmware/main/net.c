#include "net.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/ip_addr.h"
#include "mdns.h"
#include "nvs.h"
#include "ping/ping_sock.h"
#include "sdkconfig.h"

static const char *TAG = "net";

static volatile bool s_connected;
static char s_ip[16] = "";
static volatile uint32_t s_disconnects;
static volatile int s_last_reason;

#define PING_HIST 30
static int16_t s_rtt[PING_HIST];
static int s_rtt_n, s_rtt_i;
static portMUX_TYPE s_rtt_lock = portMUX_INITIALIZER_UNLOCKED;
static esp_ping_handle_t s_ping;

static void ping_record(int ms)
{
    portENTER_CRITICAL(&s_rtt_lock);
    s_rtt[s_rtt_i] = (int16_t)ms;
    s_rtt_i = (s_rtt_i + 1) % PING_HIST;
    if (s_rtt_n < PING_HIST) s_rtt_n++;
    portEXIT_CRITICAL(&s_rtt_lock);
}

static void ping_ok(esp_ping_handle_t hdl, void *args)
{
    uint32_t ms = 0;
    esp_ping_get_profile(hdl, ESP_PING_PROF_TIMEGAP, &ms, sizeof(ms));
    ping_record(ms > 30000 ? 30000 : (int)ms);
}

static void ping_lost(esp_ping_handle_t hdl, void *args)
{
    ping_record(-1);
}

static void ping_start(const esp_ip4_addr_t *gw)
{
    if (s_ping) {
        esp_ping_stop(s_ping);
        esp_ping_delete_session(s_ping);
        s_ping = NULL;
    }
    esp_ping_config_t cfg = ESP_PING_DEFAULT_CONFIG();
    cfg.target_addr.type = IPADDR_TYPE_V4;
    cfg.target_addr.u_addr.ip4.addr = gw->addr;
    cfg.count = ESP_PING_COUNT_INFINITE;
    cfg.interval_ms = 1000;
    cfg.timeout_ms = 1000;
    esp_ping_callbacks_t cbs = {.on_ping_success = ping_ok, .on_ping_timeout = ping_lost};
    if (esp_ping_new_session(&cfg, &cbs, &s_ping) == ESP_OK) esp_ping_start(s_ping);
}

void net_ping_stats(int *last, int *avg, int *loss_pct, int *samples)
{
    int sum = 0, ok = 0, lost = 0;
    portENTER_CRITICAL(&s_rtt_lock);
    int n = s_rtt_n;
    *last = n ? s_rtt[(s_rtt_i + PING_HIST - 1) % PING_HIST] : -2;
    for (int i = 0; i < n; i++) {
        if (s_rtt[i] < 0) lost++;
        else {
            sum += s_rtt[i];
            ok++;
        }
    }
    portEXIT_CRITICAL(&s_rtt_lock);
    *avg = ok ? sum / ok : -1;
    *loss_pct = n ? 100 * lost / n : 0;
    *samples = n;
}

void net_diag(int *rssi, int *channel, uint32_t *disconnects, int *reason)
{
    uint8_t bssid[6];
    net_diag_ex(rssi, channel, disconnects, reason, bssid);
}

void net_diag_ex(int *rssi, int *channel, uint32_t *disconnects, int *reason, uint8_t bssid[6])
{
    wifi_ap_record_t ap;
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
        *rssi = ap.rssi;
        *channel = ap.primary;
        memcpy(bssid, ap.bssid, 6);
    } else {
        *rssi = 0;
        *channel = 0;
        memset(bssid, 0, 6);
    }
    *disconnects = s_disconnects;
    *reason = s_last_reason;
}

#define PIN_FAILS 5
static uint8_t s_pin[6];
static bool s_has_pin, s_pin_active;
static int s_fails;

static bool pin_load(void)
{
    nvs_handle_t h;
    size_t len = sizeof(s_pin);
    if (nvs_open("net", NVS_READONLY, &h) != ESP_OK) return false;
    esp_err_t err = nvs_get_blob(h, "bssid", s_pin, &len);
    nvs_close(h);
    return err == ESP_OK && len == sizeof(s_pin);
}

static void pin_apply(bool on)
{
    wifi_config_t cfg;
    if (esp_wifi_get_config(WIFI_IF_STA, &cfg) != ESP_OK) return;
    bool want = on && s_has_pin;
    if (want == s_pin_active && (!want || memcmp(cfg.sta.bssid, s_pin, 6) == 0)) return;
    if (want) memcpy(cfg.sta.bssid, s_pin, 6);
    cfg.sta.bssid_set = want;
    s_pin_active = want;
    s_fails = 0;
    esp_wifi_set_config(WIFI_IF_STA, &cfg);
    ESP_LOGI(TAG, "access point pin %s", want ? "on" : "off");
}

void net_set_pin(const uint8_t *bssid)
{
    nvs_handle_t h;
    if (nvs_open("net", NVS_READWRITE, &h) == ESP_OK) {
        if (bssid) nvs_set_blob(h, "bssid", bssid, 6);
        else nvs_erase_key(h, "bssid");
        nvs_commit(h);
        nvs_close(h);
    }
    s_has_pin = bssid != NULL;
    if (bssid) memcpy(s_pin, bssid, 6);
    s_pin_active = !s_has_pin;
    pin_apply(true);
    esp_wifi_disconnect();
}

bool net_pinned(void)
{
    return s_pin_active;
}

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        s_connected = false;
        s_ip[0] = 0;
        s_disconnects++;
        s_last_reason = ((wifi_event_sta_disconnected_t *)data)->reason;
        ESP_LOGW(TAG, "wifi disconnected, retrying");
        if (++s_fails >= PIN_FAILS) pin_apply(false);
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = data;
        snprintf(s_ip, sizeof(s_ip), IPSTR, IP2STR(&ev->ip_info.ip));
        s_connected = true;
        s_fails = 0;
        ESP_LOGI(TAG, "connected, ip %s", s_ip);
        ping_start(&ev->ip_info.gw);
        // Ask for the time now that there is a network. Starting SNTP before
        // Wi-Fi is up leaves it in a long retry backoff after the first failure.
        if (esp_netif_sntp_start() != ESP_OK) {
            ESP_LOGW(TAG, "sntp start failed");
        }
    }
}

static void mdns_start(void)
{
    if (mdns_init() != ESP_OK) {
        ESP_LOGW(TAG, "mdns init failed");
        return;
    }
    mdns_hostname_set(CONFIG_HUB_HOSTNAME);
    mdns_instance_name_set("Desk Hub");
    mdns_service_add(NULL, "_deskhub", "_tcp", 80, NULL, 0);
}

void net_start(void)
{
    setenv("TZ", CONFIG_HUB_TZ, 1);
    tzset();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_t *netif = esp_netif_create_default_wifi_sta();
    esp_netif_set_hostname(netif, CONFIG_HUB_HOSTNAME);

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL));

    wifi_config_t cfg = {0};
    strlcpy((char *)cfg.sta.ssid, CONFIG_HUB_WIFI_SSID, sizeof(cfg.sta.ssid));
    strlcpy((char *)cfg.sta.password, CONFIG_HUB_WIFI_PASSWORD, sizeof(cfg.sta.password));
    cfg.sta.threshold.authmode = WIFI_AUTH_OPEN;
    cfg.sta.pmf_cfg.capable = true;
    cfg.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;
    s_has_pin = pin_load();
    if (s_has_pin) {
        memcpy(cfg.sta.bssid, s_pin, 6);
        cfg.sta.bssid_set = true;
        s_pin_active = true;
    }

    // Set up SNTP now but start it from the GOT_IP handler, once the network exists.
    esp_sntp_config_t sntp = ESP_NETIF_SNTP_DEFAULT_CONFIG_MULTIPLE(2, ESP_SNTP_SERVER_LIST("time.google.com", "pool.ntp.org"));
    sntp.start = false;
    esp_netif_sntp_init(&sntp);

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
    // Power save adds latency to every web socket frame; the hub is mains powered.
    esp_wifi_set_ps(WIFI_PS_NONE);

    mdns_start();
}

bool net_connected(void)
{
    return s_connected;
}

bool net_time_valid(void)
{
    return time(NULL) > 1700000000;
}

const char *net_ip(void)
{
    return s_ip;
}

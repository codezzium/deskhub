#pragma once

#include <stdbool.h>
#include <stdint.h>

// Connects to Wi-Fi, starts SNTP and advertises <hostname>.local over mDNS.
void net_start(void);
bool net_connected(void);
bool net_time_valid(void);
void net_diag(int *rssi, int *channel, uint32_t *disconnects, int *reason);
void net_diag_ex(int *rssi, int *channel, uint32_t *disconnects, int *reason, uint8_t bssid[6]);
void net_ping_stats(int *last, int *avg, int *loss_pct, int *samples);
void net_set_pin(const uint8_t *bssid);
bool net_pinned(void);
const char *net_ip(void);

#pragma once

#include <stdbool.h>

// Web socket server at ws://<host>/ws that the PC companion connects to.
void hub_server_start(void);

// True while an authenticated companion has sent something in the last few seconds.
bool hub_server_pc_online(void);

// Queue a JSON text frame for the companion. Safe from any task.
void hub_server_send(const char *json);

const char *hub_server_link(void);

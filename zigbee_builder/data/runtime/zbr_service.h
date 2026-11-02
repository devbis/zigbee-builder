/* SPDX-License-Identifier: Apache-2.0 */
/*
 * The service action of the buttons marked as service inputs: held for the configured time,
 *   in the network     leave it, erase the network (and with it everything in NV), then search;
 *   out of the network search (no erase).
 * The search runs in the window of app_bdb.c, ZBR_SEARCH_MS long, and stops when the device joins.
 */
#pragma once

/* Called by an input's on_hold, on the Zigbee thread */
void zbr_service_hold(void);

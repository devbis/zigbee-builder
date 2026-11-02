/*
 * SPDX-FileCopyrightText: Copyright The Zephyr Project Contributors
 * SPDX-License-Identifier: Apache-2.0
 */
/*
 * app_bdb.h — Commissioning / rejoin / poll-rate / leave policy for the
 * zigbee_shell sample.
 *
 * The functions declared here are the implementations of the
 * zb_platform_app_* bootstrap hooks.  main.c delegates to them so it
 * stays thin.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include <zephyr/zigbee/zb_bootstrap.h>

void app_bdb_bootstrap_ready(void);
bool app_bdb_should_start_commissioning(void);
void app_bdb_start_commissioning(void);
void app_bdb_network_left(void);
void app_bdb_commissioning_status(uint8_t status, bool joinedNetwork);
bool app_bdb_get_join_profile(struct zb_platform_bdb_join_profile *profile);

/*
 * The search window: the device looks for a network only while it is open. ms == 0 keeps it open
 * until the device joins. Closing is by time, by joining, or zbr_search_stop().
 */
void zbr_search_start(uint32_t ms);
void zbr_search_stop(void);
bool zbr_search_active(void);

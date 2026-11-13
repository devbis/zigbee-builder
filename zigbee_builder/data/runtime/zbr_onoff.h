/* SPDX-License-Identifier: Apache-2.0 */
/*
 * On/Off server driving an output (a relay). One instance per endpoint. The state
 * is kept in NV and restored by the start-up mode: 0 off, 1 on, 2 as it was.
 */
#pragma once

#include <zephyr/kernel.h>

#include "app_common.h"

struct zbr_onoff {
	uint8_t ep;
	uint8_t startup;
	uint8_t nv_item;
	void (*apply)(bool on);   /* drive the output */
	bool state;               /* the OnOff attribute points here */
	/* a pulse: on turns the output off again after this long (0: not a pulse); the state is not kept */
	uint16_t pulse_ms;
	struct k_work_delayable pulse_work;
};

void zbr_onoff_init(struct zbr_onoff *o);
void zbr_onoff_set(struct zbr_onoff *o, bool on);
status_t zbr_onoff_cmd(zclIncomingAddrInfo_t *addr_info, uint8_t cmd_id, void *payload);

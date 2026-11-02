/* SPDX-License-Identifier: Apache-2.0 */
/*
 * On/Off server driving an output (a relay). One instance per endpoint. The state
 * is kept in NV and restored by the start-up mode: 0 off, 1 on, 2 as it was.
 */
#pragma once

#include "app_common.h"

struct zbr_onoff {
	u8 ep;
	u8 startup;
	u8 nv_item;
	void (*apply)(bool on);   /* drive the output */
	bool state;               /* the OnOff attribute points here */
};

void zbr_onoff_init(struct zbr_onoff *o);
void zbr_onoff_set(struct zbr_onoff *o, bool on);
status_t zbr_onoff_cmd(zclIncomingAddrInfo_t *addr_info, u8 cmd_id, void *payload);

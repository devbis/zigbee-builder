/* SPDX-License-Identifier: Apache-2.0 */
/* On/Off client: send a command to whatever the endpoint is bound to. */
#pragma once

#include "app_common.h"

enum zbr_switch_action {
	ZBR_SWITCH_OFF,
	ZBR_SWITCH_ON,
	ZBR_SWITCH_TOGGLE,
};

void zbr_switch_send(u8 ep, enum zbr_switch_action action);

/* SPDX-License-Identifier: Apache-2.0 */
/*
 * A PWM light: up to red, green, blue, warm white and cool white channels, driven by the On/Off,
 * Level Control and Color Control clusters. The stack parses the commands; the state, the
 * transitions and the colour maths are here, in integers (no soft-float on the TC32 toolchain).
 *
 * One light per device. Outputs are linear light: brightness goes through a gamma of 2.
 * Level Control Move runs to its end and Stop is ignored; hue/saturation/colour Move and the
 * enhanced hue and colour loop commands are not implemented (answered as unsupported).
 */
#pragma once

#include <zephyr/device.h>

#include "app_common.h"
#include "zbr_onoff.h"

enum zbr_light_channel {
	ZBR_LIGHT_RED,
	ZBR_LIGHT_GREEN,
	ZBR_LIGHT_BLUE,
	ZBR_LIGHT_WARM,
	ZBR_LIGHT_COOL,
	ZBR_LIGHT_CHANNELS,
};

struct zbr_light {
	/* configuration, from the generated code */
	u8 ep;
	const struct device *pwm;
	u32 period_cycles;
	struct {
		bool present;
		bool inverted;
		u8 pwm_channel;
	} ch[ZBR_LIGHT_CHANNELS];
	u16 transition_ms;             /* On/Off transition when a command gives none */
	u16 mired_min, mired_max;      /* the physical colour temperature range */
	u8 nv_item;
	struct zbr_onoff *onoff;       /* the On/Off server of the endpoint */

	/* attributes (the generated tables point here) */
	u8 level;                      /* CurrentLevel 1..254 */
	u8 level_options;
	u8 color_mode;                 /* 0 hue/saturation, 1 xy, 2 colour temperature */
	u8 enhanced_color_mode;
	u8 hue, sat;                   /* 0..254 */
	u16 x, y;                      /* 0..65535 for 0..1 */
	u16 mireds;
	u8 color_options;

	/* private */
	bool on;
	s32 tr_override_ms;            /* -1: none */
	u16 cur[ZBR_LIGHT_CHANNELS];   /* duty in PWM cycles */
	u16 tgt[ZBR_LIGHT_CHANNELS];
	u16 steps_left;
	bool ticking, save_armed;
};

void zbr_light_init(struct zbr_light *l);
/* The apply hook of the endpoint's On/Off server */
void zbr_light_set_on(struct zbr_light *l, bool on);

status_t zbr_light_level_cmd(zclIncomingAddrInfo_t *addr_info, u8 cmd_id, void *payload);
status_t zbr_light_color_cmd(zclIncomingAddrInfo_t *addr_info, u8 cmd_id, void *payload);

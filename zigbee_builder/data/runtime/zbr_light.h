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
	uint8_t ep;
	const struct device *pwm;
	uint32_t period_cycles;
	struct {
		bool present;
		bool inverted;
		uint8_t pwm_channel;
	} ch[ZBR_LIGHT_CHANNELS];
	uint16_t transition_ms;             /* On/Off transition when a command gives none */
	uint16_t mired_min, mired_max;      /* the physical colour temperature range */
	uint8_t nv_item;
	struct zbr_onoff *onoff;       /* the On/Off server of the endpoint */

	/* attributes (the generated tables point here) */
	uint8_t level;                      /* CurrentLevel 1..254 */
	uint8_t level_options;
	uint8_t color_mode;                 /* 0 hue/saturation, 1 xy, 2 colour temperature */
	uint8_t enhanced_color_mode;
	uint8_t hue, sat;                   /* 0..254 */
	uint16_t x, y;                      /* 0..65535 for 0..1 */
	uint16_t mireds;
	uint8_t color_options;

	/* private */
	bool on;
	int32_t tr_override_ms;            /* -1: none */
	uint16_t cur[ZBR_LIGHT_CHANNELS];   /* duty in PWM cycles */
	uint16_t tgt[ZBR_LIGHT_CHANNELS];
	uint16_t steps_left;
	bool ticking, save_armed;
};

void zbr_light_init(struct zbr_light *l);
/* The apply hook of the endpoint's On/Off server */
void zbr_light_set_on(struct zbr_light *l, bool on);

status_t zbr_light_level_cmd(zclIncomingAddrInfo_t *addr_info, uint8_t cmd_id, void *payload);
status_t zbr_light_color_cmd(zclIncomingAddrInfo_t *addr_info, uint8_t cmd_id, void *payload);

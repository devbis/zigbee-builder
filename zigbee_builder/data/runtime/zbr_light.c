/* SPDX-License-Identifier: Apache-2.0 */

#include <string.h>

#include <zephyr/drivers/pwm.h>

#include "zbr_light.h"
#include "zbr_store.h"

#include "os/ev_timer.h"
#include "zb_common.h"
#include "zcl/general/zcl_level.h"
#include "zcl/light_color_control/zcl_light_colorCtrl.h"

#define TICK_MS       20U
#define SAVE_DELAY_MS 3000U

static struct zbr_light *the_light;

/* ------------------------------------------------------------------ */
/* Colour maths: every result is 0..255 linear light per channel        */
/* ------------------------------------------------------------------ */

static uint8_t mul255(uint32_t a, uint32_t b)
{
	return (uint8_t)((a * b) / 255U);
}

/* hue 0..254, saturation 0..254 at full value; the HSV of the colour wheel, gamma 2 to linear */
static void hs_to_rgb(uint8_t hue, uint8_t sat, uint8_t rgb[3])
{
	uint32_t h = (uint32_t)hue * 360U / 254U;
	uint32_t s = (uint32_t)sat * 255U / 254U;
	uint32_t region = h / 60U;
	uint32_t rem = (h % 60U) * 255U / 60U;
	uint8_t p = (uint8_t)(255U - s);
	uint8_t q = (uint8_t)(255U - mul255(s, rem));
	uint8_t t = (uint8_t)(255U - mul255(s, 255U - rem));
	uint8_t v = 255U;

	switch (region) {
	case 0:
		rgb[0] = v; rgb[1] = t; rgb[2] = p;
		break;
	case 1:
		rgb[0] = q; rgb[1] = v; rgb[2] = p;
		break;
	case 2:
		rgb[0] = p; rgb[1] = v; rgb[2] = t;
		break;
	case 3:
		rgb[0] = p; rgb[1] = q; rgb[2] = v;
		break;
	case 4:
		rgb[0] = t; rgb[1] = p; rgb[2] = v;
		break;
	default:
		rgb[0] = v; rgb[1] = p; rgb[2] = q;
		break;
	}
	for (int i = 0; i < 3; i++) {
		rgb[i] = mul255(rgb[i], rgb[i]);
	}
}

/* CIE xy (0..65535) at Y = 1 to linear sRGB, in fixed point (Q12 values, Q10 matrix), the brightest
 * channel at 255. X and Z are capped at 150 so the products stay below 2^31. */
static void xy_to_rgb(uint16_t x, uint16_t y, uint8_t rgb[3])
{
	int32_t X, Z, r, g, b, m;
	uint32_t yy = y < 655U ? 655U : y;
	int32_t rest = 65536 - (int32_t)x - (int32_t)y;

	X = (int32_t)(((uint32_t)x << 12) / yy);
	Z = (int32_t)(((uint32_t)(rest > 0 ? rest : 0) << 12) / yy);
	X = MIN(X, 150 * 4096);
	Z = MIN(Z, 150 * 4096);
	r = (3318 * X - 1574 * 4096 - 511 * Z) / 1024;
	g = (-992 * X + 1921 * 4096 + 42 * Z) / 1024;
	b = (57 * X - 209 * 4096 + 1082 * Z) / 1024;
	r = r < 0 ? 0 : r;
	g = g < 0 ? 0 : g;
	b = b < 0 ? 0 : b;
	m = MAX(r, MAX(g, b));
	if (m == 0) {
		rgb[0] = rgb[1] = rgb[2] = 0U;
		return;
	}
	while (m > 32767) {
		r >>= 1;
		g >>= 1;
		b >>= 1;
		m >>= 1;
	}
	rgb[0] = (uint8_t)(r * 255 / m);
	rgb[1] = (uint8_t)(g * 255 / m);
	rgb[2] = (uint8_t)(b * 255 / m);
}

static uint8_t lerp(uint32_t a, uint32_t b, uint32_t num, uint32_t den)
{
	return (uint8_t)((a * (den - num) + b * num) / den);
}

/* A colour temperature as RGB for lamps without white channels: 2000 K, 4000 K and 6500 K anchors */
static void mireds_to_rgb(uint16_t m, uint8_t rgb[3])
{
	static const uint8_t warm[3] = {255, 137, 18};   /* 500 mireds */
	static const uint8_t mid[3] = {255, 209, 163};   /* 250 */
	static const uint8_t cool[3] = {255, 249, 253};  /* 153 */
	const uint8_t *a, *b;
	uint32_t num, den;

	m = CLAMP(m, 153U, 500U);
	if (m >= 250U) {
		a = mid; b = warm; num = m - 250U; den = 250U;
	} else {
		a = cool; b = mid; num = m - 153U; den = 97U;
	}
	for (int i = 0; i < 3; i++) {
		rgb[i] = lerp(a[i], b[i], num, den);
	}
}

/* The target duty of every channel for the light's state */
static void compute_target(struct zbr_light *l, uint16_t out[ZBR_LIGHT_CHANNELS])
{
	uint8_t comp[ZBR_LIGHT_CHANNELS] = {0};
	bool rgb = l->ch[ZBR_LIGHT_RED].present || l->ch[ZBR_LIGHT_GREEN].present ||
		   l->ch[ZBR_LIGHT_BLUE].present;
	bool warm = l->ch[ZBR_LIGHT_WARM].present, cool = l->ch[ZBR_LIGHT_COOL].present;
	uint32_t lvl = l->on ? ((uint32_t)l->level * l->level) / 254U : 0U; /* gamma 2, 0..254 */

	if (rgb && l->color_mode != 2U) {
		uint8_t c[3];

		if (l->color_mode == 1U) {
			xy_to_rgb(l->x, l->y, c);
		} else {
			hs_to_rgb(l->hue, l->sat, c);
		}
		comp[ZBR_LIGHT_RED] = c[0];
		comp[ZBR_LIGHT_GREEN] = c[1];
		comp[ZBR_LIGHT_BLUE] = c[2];
	} else if (warm && cool) {
		uint32_t span = l->mired_max > l->mired_min ? l->mired_max - l->mired_min : 1U;
		uint32_t m = CLAMP(l->mireds, l->mired_min, l->mired_max);
		uint32_t w = (m - l->mired_min) * 255U / span;   /* more mireds: warmer */

		comp[ZBR_LIGHT_WARM] = (uint8_t)w;
		comp[ZBR_LIGHT_COOL] = (uint8_t)(255U - w);
	} else if (warm) {
		comp[ZBR_LIGHT_WARM] = 255U;
	} else if (rgb) {
		uint8_t c[3];

		mireds_to_rgb(l->mireds, c);
		comp[ZBR_LIGHT_RED] = c[0];
		comp[ZBR_LIGHT_GREEN] = c[1];
		comp[ZBR_LIGHT_BLUE] = c[2];
	}

	for (int i = 0; i < ZBR_LIGHT_CHANNELS; i++) {
		/* comp 0..255 times lvl 0..254 -> a fraction of the period */
		uint32_t d = ((uint32_t)comp[i] * lvl * (l->period_cycles / 16U)) / (255U * 254U / 16U);

		out[i] = (uint16_t)MIN(d, l->period_cycles);
	}
}

/* ------------------------------------------------------------------ */
/* Output, transitions, persistence                                     */
/* ------------------------------------------------------------------ */

static void write_outputs(struct zbr_light *l)
{
	for (int i = 0; i < ZBR_LIGHT_CHANNELS; i++) {
		if (l->ch[i].present) {
			(void)pwm_set_cycles(l->pwm, l->ch[i].pwm_channel, l->period_cycles, l->cur[i],
					     l->ch[i].inverted ? PWM_POLARITY_INVERTED : 0);
		}
	}
}

static int transition_tick(void *arg)
{
	struct zbr_light *l = arg;

	if (l->steps_left > 1U) {
		for (int i = 0; i < ZBR_LIGHT_CHANNELS; i++) {
			int32_t d = (int32_t)l->tgt[i] - (int32_t)l->cur[i];

			l->cur[i] = (uint16_t)((int32_t)l->cur[i] + d / (int32_t)l->steps_left);
		}
		l->steps_left--;
		write_outputs(l);
		return TICK_MS;
	}
	memcpy(l->cur, l->tgt, sizeof(l->cur));
	l->steps_left = 0U;
	l->ticking = false;
	write_outputs(l);
	return -1;
}

static void go(struct zbr_light *l, uint32_t transition_ms)
{
	compute_target(l, l->tgt);
	if (transition_ms < TICK_MS) {
		memcpy(l->cur, l->tgt, sizeof(l->cur));
		l->steps_left = 0U;
		write_outputs(l);
		return;
	}
	l->steps_left = (uint16_t)MIN(transition_ms / TICK_MS, 0xFFFFU);
	if (!l->ticking) {
		l->ticking = true;
		(void)ev_timer_taskPost(transition_tick, l, TICK_MS);
	}
}

struct persist {
	uint8_t level, mode, hue, sat;
	uint16_t x, y, mireds;
};

static int save_tick(void *arg)
{
	struct zbr_light *l = arg;
	struct persist p = {l->level, l->color_mode, l->hue, l->sat, l->x, l->y, l->mireds};

	zbr_store_save(l->nv_item, &p, sizeof(p));
	l->save_armed = false;
	return -1;
}

/* A slider streams commands: save once, a little after the last one */
static void save_later(struct zbr_light *l)
{
	if (!l->save_armed) {
		l->save_armed = true;
		(void)ev_timer_taskPost(save_tick, l, SAVE_DELAY_MS);
	}
}

static void publish(struct zbr_light *l)
{
	l->enhanced_color_mode = l->color_mode;
	zcl_setAttrVal(l->ep, ZCL_CLUSTER_GEN_LEVEL_CONTROL, ZCL_ATTRID_LEVEL_CURRENT_LEVEL, &l->level);
	zcl_setAttrVal(l->ep, ZCL_CLUSTER_LIGHTING_COLOR_CONTROL, ZCL_ATTRID_COLOR_MODE, &l->color_mode);
	zcl_setAttrVal(l->ep, ZCL_CLUSTER_LIGHTING_COLOR_CONTROL, ZCL_ATTRID_CURRENT_HUE, &l->hue);
	zcl_setAttrVal(l->ep, ZCL_CLUSTER_LIGHTING_COLOR_CONTROL, ZCL_ATTRID_CURRENT_SATURATION, &l->sat);
	zcl_setAttrVal(l->ep, ZCL_CLUSTER_LIGHTING_COLOR_CONTROL, ZCL_ATTRID_CURRENT_X, (uint8_t *)&l->x);
	zcl_setAttrVal(l->ep, ZCL_CLUSTER_LIGHTING_COLOR_CONTROL, ZCL_ATTRID_CURRENT_Y, (uint8_t *)&l->y);
	zcl_setAttrVal(l->ep, ZCL_CLUSTER_LIGHTING_COLOR_CONTROL, ZCL_ATTRID_COLOR_TEMPERATURE_MIREDS,
		       (uint8_t *)&l->mireds);
	zcl_setAttrVal(l->ep, ZCL_CLUSTER_LIGHTING_COLOR_CONTROL, ZCL_ATTRID_ENHANCED_COLOR_MODE,
		       &l->enhanced_color_mode);
}

void zbr_light_init(struct zbr_light *l)
{
	struct persist p;

	the_light = l;
	l->tr_override_ms = -1;
	if (l->level == 0U) {
		l->level = 254U;
	}
	if (zbr_store_load(l->nv_item, &p, sizeof(p)) && p.level >= 1U && p.level <= 254U &&
	    p.mode <= 2U) {
		l->level = p.level;
		l->color_mode = p.mode;
		l->hue = p.hue;
		l->sat = p.sat;
		l->x = p.x;
		l->y = p.y;
		l->mireds = p.mireds;
	}
	l->enhanced_color_mode = l->color_mode;
	/* the pins are set up by the PWM driver's pinctrl; start dark */
	memset(l->cur, 0, sizeof(l->cur));
	write_outputs(l);
}

void zbr_light_set_on(struct zbr_light *l, bool on)
{
	uint32_t ms = l->tr_override_ms >= 0 ? (uint32_t)l->tr_override_ms : l->transition_ms;

	l->tr_override_ms = -1;
	l->on = on;
	go(l, ms);
}

/* ------------------------------------------------------------------ */
/* Level Control                                                        */
/* ------------------------------------------------------------------ */

static void set_level(struct zbr_light *l, uint8_t level, uint32_t transition_ms, bool with_on_off)
{
	bool want_on = l->on;

	if (with_on_off) {
		want_on = level > 0U;
	}
	l->level = CLAMP(level, 1U, 254U);
	publish(l);
	save_later(l);
	l->tr_override_ms = (int32_t)transition_ms;
	if (l->onoff != NULL && with_on_off && want_on != l->on) {
		zbr_onoff_set(l->onoff, want_on);   /* applies through zbr_light_set_on */
	} else {
		l->tr_override_ms = -1;
		go(l, transition_ms);
	}
}

status_t zbr_light_level_cmd(zclIncomingAddrInfo_t *addr_info, uint8_t cmd_id, void *payload)
{
	struct zbr_light *l = the_light;
	zcl_level_cmdPayload_t *p = payload;
	bool with_on_off = false;
	int32_t target;

	ARG_UNUSED(addr_info);
	if (l == NULL) {
		return ZCL_STA_FAILURE;
	}
	switch (cmd_id) {
	case ZCL_CMD_LEVEL_MOVE_TO_LEVEL_WITH_ON_OFF:
		with_on_off = true;
		__fallthrough;
	case ZCL_CMD_LEVEL_MOVE_TO_LEVEL:
		set_level(l, p->moveToLevel.level, (uint32_t)p->moveToLevel.transitionTime * 100U, with_on_off);
		break;
	case ZCL_CMD_LEVEL_STEP_WITH_ON_OFF:
		with_on_off = true;
		__fallthrough;
	case ZCL_CMD_LEVEL_STEP:
		target = (int32_t)l->level + (p->step.stepMode == LEVEL_STEP_UP ? 1 : -1) * (int32_t)p->step.stepSize;
		set_level(l, (uint8_t)CLAMP(target, with_on_off ? 0 : 1, 254),
			  (uint32_t)p->step.transitionTime * 100U, with_on_off);
		break;
	case ZCL_CMD_LEVEL_MOVE_WITH_ON_OFF:
		with_on_off = true;
		__fallthrough;
	case ZCL_CMD_LEVEL_MOVE: {
		uint32_t rate = p->move.rate ? p->move.rate : 1U;
		uint8_t end = p->move.moveMode == LEVEL_MOVE_UP ? 254U : (with_on_off ? 0U : 1U);
		uint32_t dist = end > l->level ? end - l->level : l->level - end;

		set_level(l, end, dist * 1000U / rate, with_on_off);
		break;
	}
	case ZCL_CMD_LEVEL_STOP:
	case ZCL_CMD_LEVEL_STOP_WITH_ON_OFF:
		break;
	default:
		return ZCL_STA_UNSUP_CLUSTER_COMMAND;
	}
	return ZCL_STA_SUCCESS;
}

/* ------------------------------------------------------------------ */
/* Color Control                                                        */
/* ------------------------------------------------------------------ */

static uint8_t step_u8(uint8_t v, uint8_t mode, uint8_t size)
{
	return (uint8_t)CLAMP((int32_t)v + (mode == COLOR_CTRL_STEP_MODE_UP ? 1 : -1) * (int32_t)size, 0, 254);
}

status_t zbr_light_color_cmd(zclIncomingAddrInfo_t *addr_info, uint8_t cmd_id, void *payload)
{
	struct zbr_light *l = the_light;
	zcl_colorCtrl_cmdPayload_t *p = payload;
	uint32_t ms = 0U;

	ARG_UNUSED(addr_info);
	if (l == NULL) {
		return ZCL_STA_FAILURE;
	}
	switch (cmd_id) {
	case ZCL_CMD_LIGHT_COLOR_CONTROL_MOVE_TO_HUE:
		l->hue = MIN(p->moveToHue.hue, 254U);
		l->color_mode = 0U;
		ms = (uint32_t)p->moveToHue.transitionTime * 100U;
		break;
	case ZCL_CMD_LIGHT_COLOR_CONTROL_MOVE_TO_SATURATION:
		l->sat = MIN(p->moveToSaturation.saturation, 254U);
		l->color_mode = 0U;
		ms = (uint32_t)p->moveToSaturation.transitionTime * 100U;
		break;
	case ZCL_CMD_LIGHT_COLOR_CONTROL_MOVE_TO_HUE_AND_SATURATION:
		l->hue = MIN(p->moveToHueAndSaturation.hue, 254U);
		l->sat = MIN(p->moveToHueAndSaturation.saturation, 254U);
		l->color_mode = 0U;
		ms = (uint32_t)p->moveToHueAndSaturation.transitionTime * 100U;
		break;
	case ZCL_CMD_LIGHT_COLOR_CONTROL_STEP_HUE:
		l->hue = step_u8(l->hue, p->stepHue.stepMode, p->stepHue.stepSize);
		l->color_mode = 0U;
		ms = (uint32_t)p->stepHue.transitionTime * 100U;
		break;
	case ZCL_CMD_LIGHT_COLOR_CONTROL_STEP_SATURATION:
		l->sat = step_u8(l->sat, p->stepSaturation.stepMode, p->stepSaturation.stepSize);
		l->color_mode = 0U;
		ms = (uint32_t)p->stepSaturation.transitionTime * 100U;
		break;
	case ZCL_CMD_LIGHT_COLOR_CONTROL_MOVE_TO_COLOR:
		l->x = p->moveToColor.colorX;
		l->y = p->moveToColor.colorY;
		l->color_mode = 1U;
		ms = (uint32_t)p->moveToColor.transitionTime * 100U;
		break;
	case ZCL_CMD_LIGHT_COLOR_CONTROL_STEP_COLOR:
		l->x = (uint16_t)CLAMP((int32_t)l->x + p->stepColor.stepX, 0, 65279);
		l->y = (uint16_t)CLAMP((int32_t)l->y + p->stepColor.stepY, 0, 65279);
		l->color_mode = 1U;
		ms = (uint32_t)p->stepColor.transitionTime * 100U;
		break;
	case ZCL_CMD_LIGHT_COLOR_CONTROL_MOVE_TO_COLOR_TEMPERATURE:
		l->mireds = CLAMP(p->moveToColorTemperature.colorTemperature, l->mired_min, l->mired_max);
		l->color_mode = 2U;
		ms = (uint32_t)p->moveToColorTemperature.transitionTime * 100U;
		break;
	case ZCL_CMD_LIGHT_COLOR_CONTROL_STEP_COLOR_TEMPERATURE: {
		int32_t d = (int32_t)p->stepColorTemperature.stepSize *
			(p->stepColorTemperature.stepMode == COLOR_CTRL_STEP_MODE_UP ? 1 : -1);

		l->mireds = (uint16_t)CLAMP((int32_t)l->mireds + d, l->mired_min, l->mired_max);
		l->color_mode = 2U;
		ms = (uint32_t)p->stepColorTemperature.transitionTime * 100U;
		break;
	}
	default:
		return ZCL_STA_UNSUP_CLUSTER_COMMAND;
	}
	publish(l);
	save_later(l);
	go(l, ms);
	return ZCL_STA_SUCCESS;
}

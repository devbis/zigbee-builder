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

static u8 mul255(u32 a, u32 b)
{
	return (u8)((a * b) / 255U);
}

/* hue 0..254, saturation 0..254 at full value; the HSV of the colour wheel, gamma 2 to linear */
static void hs_to_rgb(u8 hue, u8 sat, u8 rgb[3])
{
	u32 h = (u32)hue * 360U / 254U;
	u32 s = (u32)sat * 255U / 254U;
	u32 region = h / 60U;
	u32 rem = (h % 60U) * 255U / 60U;
	u8 p = (u8)(255U - s);
	u8 q = (u8)(255U - mul255(s, rem));
	u8 t = (u8)(255U - mul255(s, 255U - rem));
	u8 v = 255U;

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
static void xy_to_rgb(u16 x, u16 y, u8 rgb[3])
{
	s32 X, Z, r, g, b, m;
	u32 yy = y < 655U ? 655U : y;
	s32 rest = 65536 - (s32)x - (s32)y;

	X = (s32)(((u32)x << 12) / yy);
	Z = (s32)(((u32)(rest > 0 ? rest : 0) << 12) / yy);
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
	rgb[0] = (u8)(r * 255 / m);
	rgb[1] = (u8)(g * 255 / m);
	rgb[2] = (u8)(b * 255 / m);
}

static u8 lerp(u32 a, u32 b, u32 num, u32 den)
{
	return (u8)((a * (den - num) + b * num) / den);
}

/* A colour temperature as RGB for lamps without white channels: 2000 K, 4000 K and 6500 K anchors */
static void mireds_to_rgb(u16 m, u8 rgb[3])
{
	static const u8 warm[3] = {255, 137, 18};   /* 500 mireds */
	static const u8 mid[3] = {255, 209, 163};   /* 250 */
	static const u8 cool[3] = {255, 249, 253};  /* 153 */
	const u8 *a, *b;
	u32 num, den;

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
static void compute_target(struct zbr_light *l, u16 out[ZBR_LIGHT_CHANNELS])
{
	u8 comp[ZBR_LIGHT_CHANNELS] = {0};
	bool rgb = l->ch[ZBR_LIGHT_RED].present || l->ch[ZBR_LIGHT_GREEN].present ||
		   l->ch[ZBR_LIGHT_BLUE].present;
	bool warm = l->ch[ZBR_LIGHT_WARM].present, cool = l->ch[ZBR_LIGHT_COOL].present;
	u32 lvl = l->on ? ((u32)l->level * l->level) / 254U : 0U; /* gamma 2, 0..254 */

	if (rgb && l->color_mode != 2U) {
		u8 c[3];

		if (l->color_mode == 1U) {
			xy_to_rgb(l->x, l->y, c);
		} else {
			hs_to_rgb(l->hue, l->sat, c);
		}
		comp[ZBR_LIGHT_RED] = c[0];
		comp[ZBR_LIGHT_GREEN] = c[1];
		comp[ZBR_LIGHT_BLUE] = c[2];
	} else if (warm && cool) {
		u32 span = l->mired_max > l->mired_min ? l->mired_max - l->mired_min : 1U;
		u32 m = CLAMP(l->mireds, l->mired_min, l->mired_max);
		u32 w = (m - l->mired_min) * 255U / span;   /* more mireds: warmer */

		comp[ZBR_LIGHT_WARM] = (u8)w;
		comp[ZBR_LIGHT_COOL] = (u8)(255U - w);
	} else if (warm) {
		comp[ZBR_LIGHT_WARM] = 255U;
	} else if (rgb) {
		u8 c[3];

		mireds_to_rgb(l->mireds, c);
		comp[ZBR_LIGHT_RED] = c[0];
		comp[ZBR_LIGHT_GREEN] = c[1];
		comp[ZBR_LIGHT_BLUE] = c[2];
	}

	for (int i = 0; i < ZBR_LIGHT_CHANNELS; i++) {
		/* comp 0..255 times lvl 0..254 -> a fraction of the period */
		u32 d = ((u32)comp[i] * lvl * (l->period_cycles / 16U)) / (255U * 254U / 16U);

		out[i] = (u16)MIN(d, l->period_cycles);
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
			s32 d = (s32)l->tgt[i] - (s32)l->cur[i];

			l->cur[i] = (u16)((s32)l->cur[i] + d / (s32)l->steps_left);
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

static void go(struct zbr_light *l, u32 transition_ms)
{
	compute_target(l, l->tgt);
	if (transition_ms < TICK_MS) {
		memcpy(l->cur, l->tgt, sizeof(l->cur));
		l->steps_left = 0U;
		write_outputs(l);
		return;
	}
	l->steps_left = (u16)MIN(transition_ms / TICK_MS, 0xFFFFU);
	if (!l->ticking) {
		l->ticking = true;
		(void)ev_timer_taskPost(transition_tick, l, TICK_MS);
	}
}

struct persist {
	u8 level, mode, hue, sat;
	u16 x, y, mireds;
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
	zcl_setAttrVal(l->ep, ZCL_CLUSTER_LIGHTING_COLOR_CONTROL, ZCL_ATTRID_CURRENT_X, (u8 *)&l->x);
	zcl_setAttrVal(l->ep, ZCL_CLUSTER_LIGHTING_COLOR_CONTROL, ZCL_ATTRID_CURRENT_Y, (u8 *)&l->y);
	zcl_setAttrVal(l->ep, ZCL_CLUSTER_LIGHTING_COLOR_CONTROL, ZCL_ATTRID_COLOR_TEMPERATURE_MIREDS,
		       (u8 *)&l->mireds);
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
	u32 ms = l->tr_override_ms >= 0 ? (u32)l->tr_override_ms : l->transition_ms;

	l->tr_override_ms = -1;
	l->on = on;
	go(l, ms);
}

/* ------------------------------------------------------------------ */
/* Level Control                                                        */
/* ------------------------------------------------------------------ */

static void set_level(struct zbr_light *l, u8 level, u32 transition_ms, bool with_on_off)
{
	bool want_on = l->on;

	if (with_on_off) {
		want_on = level > 0U;
	}
	l->level = CLAMP(level, 1U, 254U);
	publish(l);
	save_later(l);
	l->tr_override_ms = (s32)transition_ms;
	if (l->onoff != NULL && with_on_off && want_on != l->on) {
		zbr_onoff_set(l->onoff, want_on);   /* applies through zbr_light_set_on */
	} else {
		l->tr_override_ms = -1;
		go(l, transition_ms);
	}
}

status_t zbr_light_level_cmd(zclIncomingAddrInfo_t *addr_info, u8 cmd_id, void *payload)
{
	struct zbr_light *l = the_light;
	zcl_level_cmdPayload_t *p = payload;
	bool with_on_off = false;
	s32 target;

	ARG_UNUSED(addr_info);
	if (l == NULL) {
		return ZCL_STA_FAILURE;
	}
	switch (cmd_id) {
	case ZCL_CMD_LEVEL_MOVE_TO_LEVEL_WITH_ON_OFF:
		with_on_off = true;
		__fallthrough;
	case ZCL_CMD_LEVEL_MOVE_TO_LEVEL:
		set_level(l, p->moveToLevel.level, (u32)p->moveToLevel.transitionTime * 100U, with_on_off);
		break;
	case ZCL_CMD_LEVEL_STEP_WITH_ON_OFF:
		with_on_off = true;
		__fallthrough;
	case ZCL_CMD_LEVEL_STEP:
		target = (s32)l->level + (p->step.stepMode == LEVEL_STEP_UP ? 1 : -1) * (s32)p->step.stepSize;
		set_level(l, (u8)CLAMP(target, with_on_off ? 0 : 1, 254),
			  (u32)p->step.transitionTime * 100U, with_on_off);
		break;
	case ZCL_CMD_LEVEL_MOVE_WITH_ON_OFF:
		with_on_off = true;
		__fallthrough;
	case ZCL_CMD_LEVEL_MOVE: {
		u32 rate = p->move.rate ? p->move.rate : 1U;
		u8 end = p->move.moveMode == LEVEL_MOVE_UP ? 254U : (with_on_off ? 0U : 1U);
		u32 dist = end > l->level ? end - l->level : l->level - end;

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

static u8 step_u8(u8 v, u8 mode, u8 size)
{
	return (u8)CLAMP((s32)v + (mode == COLOR_CTRL_STEP_MODE_UP ? 1 : -1) * (s32)size, 0, 254);
}

status_t zbr_light_color_cmd(zclIncomingAddrInfo_t *addr_info, u8 cmd_id, void *payload)
{
	struct zbr_light *l = the_light;
	zcl_colorCtrl_cmdPayload_t *p = payload;
	u32 ms = 0U;

	ARG_UNUSED(addr_info);
	if (l == NULL) {
		return ZCL_STA_FAILURE;
	}
	switch (cmd_id) {
	case ZCL_CMD_LIGHT_COLOR_CONTROL_MOVE_TO_HUE:
		l->hue = MIN(p->moveToHue.hue, 254U);
		l->color_mode = 0U;
		ms = (u32)p->moveToHue.transitionTime * 100U;
		break;
	case ZCL_CMD_LIGHT_COLOR_CONTROL_MOVE_TO_SATURATION:
		l->sat = MIN(p->moveToSaturation.saturation, 254U);
		l->color_mode = 0U;
		ms = (u32)p->moveToSaturation.transitionTime * 100U;
		break;
	case ZCL_CMD_LIGHT_COLOR_CONTROL_MOVE_TO_HUE_AND_SATURATION:
		l->hue = MIN(p->moveToHueAndSaturation.hue, 254U);
		l->sat = MIN(p->moveToHueAndSaturation.saturation, 254U);
		l->color_mode = 0U;
		ms = (u32)p->moveToHueAndSaturation.transitionTime * 100U;
		break;
	case ZCL_CMD_LIGHT_COLOR_CONTROL_STEP_HUE:
		l->hue = step_u8(l->hue, p->stepHue.stepMode, p->stepHue.stepSize);
		l->color_mode = 0U;
		ms = (u32)p->stepHue.transitionTime * 100U;
		break;
	case ZCL_CMD_LIGHT_COLOR_CONTROL_STEP_SATURATION:
		l->sat = step_u8(l->sat, p->stepSaturation.stepMode, p->stepSaturation.stepSize);
		l->color_mode = 0U;
		ms = (u32)p->stepSaturation.transitionTime * 100U;
		break;
	case ZCL_CMD_LIGHT_COLOR_CONTROL_MOVE_TO_COLOR:
		l->x = p->moveToColor.colorX;
		l->y = p->moveToColor.colorY;
		l->color_mode = 1U;
		ms = (u32)p->moveToColor.transitionTime * 100U;
		break;
	case ZCL_CMD_LIGHT_COLOR_CONTROL_STEP_COLOR:
		l->x = (u16)CLAMP((s32)l->x + p->stepColor.stepX, 0, 65279);
		l->y = (u16)CLAMP((s32)l->y + p->stepColor.stepY, 0, 65279);
		l->color_mode = 1U;
		ms = (u32)p->stepColor.transitionTime * 100U;
		break;
	case ZCL_CMD_LIGHT_COLOR_CONTROL_MOVE_TO_COLOR_TEMPERATURE:
		l->mireds = CLAMP(p->moveToColorTemperature.colorTemperature, l->mired_min, l->mired_max);
		l->color_mode = 2U;
		ms = (u32)p->moveToColorTemperature.transitionTime * 100U;
		break;
	case ZCL_CMD_LIGHT_COLOR_CONTROL_STEP_COLOR_TEMPERATURE: {
		s32 d = (s32)p->stepColorTemperature.stepSize *
			(p->stepColorTemperature.stepMode == COLOR_CTRL_STEP_MODE_UP ? 1 : -1);

		l->mireds = (u16)CLAMP((s32)l->mireds + d, l->mired_min, l->mired_max);
		l->color_mode = 2U;
		ms = (u32)p->stepColorTemperature.transitionTime * 100U;
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

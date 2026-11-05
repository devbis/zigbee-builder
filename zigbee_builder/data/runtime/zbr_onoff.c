/* SPDX-License-Identifier: Apache-2.0 */

#include "zbr_onoff.h"
#include "zbr_store.h"

#include "zbr_input.h"

#include "zb_common.h"
#include "zbapi/zb_api.h"
#include "zcl/general/zcl_onoff.h"

#define MAX_INSTANCES 8U

static struct zbr_onoff *inst[MAX_INSTANCES];
static size_t n_inst;

/* The pulse is over: back on the Zigbee thread, where the attribute may be set */
static void pulse_task(void *arg)
{
	zbr_onoff_set(arg, false);
}

static void pulse_over(struct k_work *work)
{
	struct zbr_onoff *o = CONTAINER_OF(k_work_delayable_from_work(work), struct zbr_onoff, pulse_work);

	(void)tl_zbTaskPost(pulse_task, o);
}

void zbr_onoff_init(struct zbr_onoff *o)
{
	u8 prev = 0U;

	if (n_inst < MAX_INSTANCES) {
		inst[n_inst++] = o;
	}
	(void)zbr_store_load(o->nv_item, &prev, 1);
	o->state = (o->startup == 2U) ? (prev != 0U) : (o->startup == 1U);
	if (o->pulse_ms != 0U) {
		k_work_init_delayable(&o->pulse_work, pulse_over);
		o->state = false;
	}
	o->apply(o->state);
}

void zbr_onoff_set(struct zbr_onoff *o, bool on)
{
	o->state = on;
	o->apply(on);
	zcl_setAttrVal(o->ep, ZCL_CLUSTER_GEN_ON_OFF, ZCL_ATTRID_ONOFF, (u8 *)&o->state);
	if (o->pulse_ms != 0U) {
		if (on) {
			/* on again inside a pulse starts it over */
			zbr_stay_awake((uint32_t)o->pulse_ms + 2000U);
			(void)k_work_reschedule(&o->pulse_work, K_MSEC(o->pulse_ms));
		} else {
			(void)k_work_cancel_delayable(&o->pulse_work);
		}
		return;                   /* a pulse is not remembered over a reset */
	}
	if (o->startup == 2U) {
		u8 v = on;

		zbr_store_save(o->nv_item, &v, 1);
	}
}

status_t zbr_onoff_cmd(zclIncomingAddrInfo_t *addr_info, u8 cmd_id, void *payload)
{
	struct zbr_onoff *o = NULL;

	ARG_UNUSED(payload);
	for (size_t i = 0; i < n_inst; i++) {
		if (inst[i]->ep == addr_info->dstEp) {
			o = inst[i];
		}
	}
	if (o == NULL) {
		return ZCL_STA_FAILURE;
	}
	switch (cmd_id) {
	case ZCL_CMD_ONOFF_ON:
		zbr_onoff_set(o, true);
		break;
	case ZCL_CMD_ONOFF_OFF:
		zbr_onoff_set(o, false);
		break;
	case ZCL_CMD_ONOFF_TOGGLE:
		zbr_onoff_set(o, !o->state);
		break;
	default:
		return ZCL_STA_UNSUP_CLUSTER_COMMAND;
	}
	return ZCL_STA_SUCCESS;
}

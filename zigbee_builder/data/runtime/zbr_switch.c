/* SPDX-License-Identifier: Apache-2.0 */

#include <string.h>

#include "zbr_switch.h"
#include "zbr_input.h"

#include "zb_common.h"
#include "zbapi/zb_api.h"
#include "zcl/general/zcl_onoff.h"

void zbr_switch_send(u8 ep, enum zbr_switch_action action)
{
	epInfo_t dst;

	if (!zb_isDeviceJoinedNwk() || !zb_bindingTblSearched(ZCL_CLUSTER_GEN_ON_OFF, ep)) {
		return;
	}
	memset(&dst, 0, sizeof(dst));
	dst.dstAddrMode = APS_DSTADDR_EP_NOTPRESETNT;
	dst.profileId = APP_HA_PROFILE_ID;

	switch (action) {
	case ZBR_SWITCH_ON:
		(void)zcl_onOff_onCmd(ep, &dst, TRUE);
		break;
	case ZBR_SWITCH_OFF:
		(void)zcl_onOff_offCmd(ep, &dst, TRUE);
		break;
	default:
		(void)zcl_onOff_toggleCmd(ep, &dst, TRUE);
		break;
	}
	/* The binding's targets may answer or poll us: stay up for the fast poll window */
	zbr_stay_awake(2000);
}

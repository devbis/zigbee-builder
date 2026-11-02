/* SPDX-License-Identifier: Apache-2.0 */

#include <string.h>

#include "zbr_ias.h"
#include "zbr_input.h"

#include "drv_nv.h"
#include "zb_common.h"
#include "zbapi/zb_api.h"
#include "zcl/security_safety/zcl_ias_zone.h"

#define NV_ITEM_BASE 0x80U
#define NV_VERSION   1U
#define MAX_ZONES    4U

struct nv_rec {
	u8 version;
	u8 zone_state;
	u8 zone_id;
	u8 cie_addr[8];
	u16 zone_status;
	u8 crc;
};

static struct zbr_ias *zones[MAX_ZONES];
static size_t n_zones;

static u8 crc8(const u8 *p, size_t n)
{
	u8 crc = 0U;

	while (n-- > 0U) {
		crc ^= *p++;
		for (int i = 0; i < 8; i++) {
			crc = (crc & 0x80U) ? (u8)((crc << 1) ^ 0x07U) : (u8)(crc << 1);
		}
	}
	return crc;
}

static void save(const struct zbr_ias *z)
{
	struct nv_rec r;

	memset(&r, 0, sizeof(r));
	r.version = NV_VERSION;
	r.zone_state = z->zone_state;
	r.zone_id = z->zone_id;
	memcpy(r.cie_addr, z->cie_addr, sizeof(r.cie_addr));
	r.zone_status = z->zone_status;
	r.crc = crc8((const u8 *)&r, offsetof(struct nv_rec, crc));
	/* NVS skips a write of what it already holds */
	(void)nv_flashWriteNew(1, NV_MODULE_APP, NV_ITEM_BASE + z->nv_slot, sizeof(r), (u8 *)&r);
}

void zbr_ias_init(struct zbr_ias *z)
{
	struct nv_rec r;

	z->zone_state = ZONE_STATE_NOT_ENROLLED;
	z->zone_id = ZCL_ZONE_ID_INVALID;
	z->zone_status = 0U;
	memset(z->cie_addr, 0, sizeof(z->cie_addr));

	if (nv_flashReadNew(1, NV_MODULE_APP, NV_ITEM_BASE + z->nv_slot, sizeof(r), (u8 *)&r) ==
		    NV_SUCC &&
	    r.version == NV_VERSION && r.crc == crc8((const u8 *)&r, offsetof(struct nv_rec, crc))) {
		z->zone_state = r.zone_state;
		z->zone_id = r.zone_id;
		memcpy(z->cie_addr, r.cie_addr, sizeof(z->cie_addr));
		z->zone_status = r.zone_status;
	}
	if (n_zones < MAX_ZONES) {
		zones[n_zones++] = z;
	}
}

static bool cie_set(const struct zbr_ias *z)
{
	for (size_t i = 0; i < sizeof(z->cie_addr); i++) {
		if (z->cie_addr[i] != 0U) {
			return true;
		}
	}
	return false;
}

static void dst_unicast(epInfo_t *dst, u16 addr, u8 ep)
{
	memset(dst, 0, sizeof(*dst));
	dst->dstAddrMode = APS_SHORT_DSTADDR_WITHEP;
	dst->dstAddr.shortAddr = addr;
	dst->dstEp = ep;
	dst->profileId = APP_HA_PROFILE_ID;
}

static bool notify(struct zbr_ias *z)
{
	zoneStatusChangeNoti_t noti = {0};
	epInfo_t dst;

	if (!zb_isDeviceJoinedNwk() || z->zone_state != ZONE_STATE_ENROLLED) {
		return false;
	}
	noti.zoneStatus = z->zone_status;
	noti.zoneId = z->zone_id;
	/* The CIE is the coordinator: 0x0000, endpoint 1 */
	dst_unicast(&dst, 0x0000U, 0x01U);
	(void)zcl_iasZone_statusChangeNotificationCmd(z->ep, &dst, TRUE, &noti);
	zbr_stay_awake(2000);
	return true;
}

void zbr_ias_set(struct zbr_ias *z, u16 mask, bool on)
{
	u16 old = z->zone_status;

	z->zone_status = on ? (u16)(old | mask) : (u16)(old & ~mask);
	zcl_setAttrVal(z->ep, ZCL_CLUSTER_SS_IAS_ZONE, ZCL_ATTRID_ZONE_STATUS, (u8 *)&z->zone_status);
	if (z->zone_status == old) {
		return;
	}
	save(z);
	z->notify_pending = !notify(z);
}

void zbr_ias_poll(void)
{
	for (size_t i = 0; i < n_zones; i++) {
		if (zones[i]->notify_pending && notify(zones[i])) {
			zones[i]->notify_pending = false;
		}
	}
}

bool zbr_ias_pending(void)
{
	for (size_t i = 0; i < n_zones; i++) {
		/* not enrolled: nothing to wait for, the CIE enrolls us on its own time */
		if (zones[i]->notify_pending && zones[i]->zone_state == ZONE_STATE_ENROLLED) {
			return true;
		}
	}
	return false;
}

static struct zbr_ias *by_ep(u8 ep)
{
	for (size_t i = 0; i < n_zones; i++) {
		if (zones[i]->ep == ep) {
			return zones[i];
		}
	}
	return NULL;
}

status_t zbr_ias_cmd(zclIncomingAddrInfo_t *addr_info, u8 cmd_id, void *payload)
{
	struct zbr_ias *z = by_ep(addr_info->dstEp);

	if (z == NULL) {
		return ZCL_STA_FAILURE;
	}
	switch (cmd_id) {
	case ZCL_CMD_ZONE_ENROLL_RSP: {
		zoneEnrollRsp_t *rsp = payload;

		if (rsp->code == 0U) {
			z->zone_id = rsp->zoneId;
			z->zone_state = ZONE_STATE_ENROLLED;
			zcl_setAttrVal(z->ep, ZCL_CLUSTER_SS_IAS_ZONE, ZCL_ATTRID_ZONE_ID, &z->zone_id);
			zcl_setAttrVal(z->ep, ZCL_CLUSTER_SS_IAS_ZONE, ZCL_ATTRID_ZONE_STATE,
				       &z->zone_state);
			save(z);
			/* Tell the state the zone is in right now */
			z->notify_pending = !notify(z);
		}
		break;
	}
	case ZCL_CMD_INIT_NORMAL_OPERATION_MODE:
	case ZCL_CMD_INIT_TEST_MODE:
		break;
	default:
		return ZCL_STA_UNSUP_CLUSTER_COMMAND;
	}
	return ZCL_STA_SUCCESS;
}

void zbr_ias_hook(zclIncoming_t *in)
{
	struct zbr_ias *z;
	zoneEnrollReq_t req = {.manufacturerCode = 0xFFFFU};
	epInfo_t dst;

	if (in->hdr.cmd != ZCL_CMD_WRITE || in->msg->indInfo.cluster_id != ZCL_CLUSTER_SS_IAS_ZONE) {
		return;
	}
	z = by_ep(in->msg->indInfo.dst_ep);
	if (z == NULL || z->zone_state == ZONE_STATE_ENROLLED || !cie_set(z)) {
		return;
	}
	req.zoneType = z->zone_type;
	save(z);
	dst_unicast(&dst, in->msg->indInfo.src_short_addr, in->msg->indInfo.src_ep);
	(void)zcl_iasZone_enrollReqCmd(z->ep, &dst, TRUE, &req);
	zbr_stay_awake(5000);
}

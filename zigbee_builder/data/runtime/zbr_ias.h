/* SPDX-License-Identifier: Apache-2.0 */
/*
 * IAS Zone server (door, leak, motion, smoke ... sensors). One instance per
 * endpoint. The enrollment (CIE address, zone id, state) and the zone status
 * live in NV: a deep-sleeping device is reset on every wake. A change of the
 * status is sent as Zone Status Change Notification as soon as the device is
 * joined and enrolled; zbr_ias_pending() tells that one still waits.
 */
#pragma once

#include "app_common.h"

struct zbr_ias {
	u8 ep;
	u16 zone_type;
	u8 nv_slot;
	/* attributes, the generated table points here */
	u8 zone_state;
	u16 zone_status;
	u8 zone_id;
	u8 cie_addr[8];
	/* private */
	bool notify_pending;
};

void zbr_ias_init(struct zbr_ias *z);

/* Set or clear status bits (ZONE_STATUS_BIT_*); notifies when the status changed */
void zbr_ias_set(struct zbr_ias *z, u16 mask, bool on);

/* Cluster command callback: Zone Enroll Response */
status_t zbr_ias_cmd(zclIncomingAddrInfo_t *addr_info, u8 cmd_id, void *payload);

/* The network was left and the NV erased: the enrollment is void, the zone is not enrolled any more */
void zbr_ias_reset(struct zbr_ias *z);

/* Call from the ZCL hook: a write of the CIE address starts the enrollment */
void zbr_ias_hook(zclIncoming_t *in);

/* Retry a notification that could not go out (not joined, not enrolled yet) */
void zbr_ias_poll(void);

/* True while a notification waits for the network; a sleepy device must not sleep */
bool zbr_ias_pending(void);

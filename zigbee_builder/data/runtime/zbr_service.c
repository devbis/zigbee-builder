/* SPDX-License-Identifier: Apache-2.0 */

#include <string.h>

#include <zephyr/kernel.h>

#include "zbr_service.h"
#include "app_bdb.h"
#include "app_common.h"
#include "zb_gen.h"

#include "zb_common.h"
#include "zbapi/zb_api.h"

/*
 * zb_factoryReset() sends a leave and waits for its confirmation. A router's own leave is a
 * broadcast, and the confirmation of a broadcast carries the invalid address (all 0xff);
 * zdo_nlme_leave_confirm_cb() acts only on the zero address or the device's own, so it never
 * ends the leave: the network stays in the NV and the device in it. The same goes for a leave
 * ordered by the coordinator.
 * Give it the confirmation it understands (the zero address: this device), the rest of the
 * leave is the stack's: stop the timers, erase the NV, reset, call zb_platform_network_left().
 */
/* zdo/zdo_nwk_manager.h is not on the include path of the application */
extern void zdo_nlme_leave_confirm_cb(void *arg);

static void leave_now(void)
{
	zb_buf_t *buf = zb_buf_allocate();

	if (buf == NULL) {
		zb_factoryReset();
		return;
	}
	memset(buf, 0, 16);                    /* nlme_leave_cnf_t: the address (8) and the status */
	buf->hdr.leaveRejoin = 0U;
	(void)tl_zbTaskPost(zdo_nlme_leave_confirm_cb, buf);
}

void zbr_service_hold(void)
{
	/* The window first: the leave below ends in app_bdb_network_left(), which searches in a window */
	zbr_search_start(ZBR_SEARCH_MS);

	if (zb_isDeviceJoinedNwk()) {
		leave_now();
	}
}

/* Overridden by the generated code, which knows what to reset */
__weak void zbr_network_left(void)
{
}

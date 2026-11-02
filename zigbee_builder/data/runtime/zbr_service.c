/* SPDX-License-Identifier: Apache-2.0 */

#include <zephyr/kernel.h>

#include "zbr_service.h"
#include "app_bdb.h"
#include "app_common.h"
#include "zb_gen.h"

#include "zb_common.h"
#include "zbapi/zb_api.h"

void zbr_service_hold(void)
{
	/* The window first: the leave below ends in app_bdb_network_left(), which searches in a window */
	zbr_search_start(ZBR_SEARCH_MS);

	if (zb_isDeviceJoinedNwk()) {
		zb_factoryReset();
	}
}

/* Overridden by the generated code, which knows what to reset */
__weak void zbr_network_left(void)
{
}

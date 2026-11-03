/*
 * SPDX-FileCopyrightText: Copyright The Zephyr Project Contributors
 * SPDX-License-Identifier: Apache-2.0
 */
/*
 * app_bdb.c — Commissioning / rejoin / poll-rate / leave policy.
 *
 * Copied from samples/zigbee/zigbee_shell of the Zephyr fork and changed in one respect: the search
 * for a network does not start by itself. It runs in a search window, opened by zbr_search_start()
 * (the service button, see zbr_service.c) and closed after its time, or when the device joins. The
 * device out of the network and with no open window does nothing: a battery device can sleep.
 * A device with no service input opens an unlimited window at boot (zbr_search_start(0)), the
 * behaviour of the sample.
 */

#include "app_bdb.h"
#include "app_common.h"
#if __has_include("zb_gen.h")
#include "zb_gen.h"
#endif
#include "app_profile.h"

#include <string.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/zigbee/zb_bootstrap.h>
#include <zephyr/zigbee/zb_channel.h>
#include <zephyr/zigbee/zb_config.h>

#if !defined(ZDO_ZCL_STUBS_H_)
/*
 * Production build: pull in the real SDK headers for ZDO/ZCL types and
 * constants.  In the host test harness zdo_zcl_stubs.h is pre-included
 * before app_bdb.c, so this block is deliberately skipped there.
 */
#include "zbapi/zb_api.h"
#include "zcl/zcl_config.h"
#include "zcl/zcl.h"
#endif

LOG_MODULE_REGISTER(app_bdb);

#if defined(CONFIG_ZIGBEE_ROUTER) || defined(CONFIG_ZIGBEE_COORDINATOR)
#define APP_BDB_ROLE_ROUTER 1
#define APP_BDB_ROLE_ED     0
#else
#define APP_BDB_ROLE_ROUTER 0
#define APP_BDB_ROLE_ED     1
#endif

typedef int (*app_bdb_timer_callback_t)(void *data);

struct ev_timer_event_t;

extern struct ev_timer_event_t *ev_timer_taskPost(app_bdb_timer_callback_t func, void *arg,
						  uint32_t t_ms);
extern uint8_t ev_timer_taskCancel(struct ev_timer_event_t **evt);
extern uint8_t zb_routerStart(void);
#if APP_BDB_ROLE_ROUTER
/*
 * Open the FFD as a parent after it joins so other devices can join through
 * it. 0xff = permit-join permanently.
 */
extern void zb_router_enable_parenting(uint8_t permit_duration);
#endif

#define APP_BDB_COMMISSIONING_RETRY_MS 5000U

#if defined(CONFIG_ZIGBEE_BDB)
static bool commissioning_start_requested;
static bool bdb_runtime_ready;
static bool leave_recommission_pending;
static struct ev_timer_event_t *commissioning_retry_timer;
static bool search_open;                       /* the search window */
static bool power_on_checked;                  /* the search on power-on is decided once per boot */
static struct ev_timer_event_t *search_timer;  /* closes it */
static const uint8_t app_bdb_fixed_tc_addr[8] = {
	0x0c, 0x80, 0x1e, 0xfe, 0xff, 0x16, 0xa7, 0x20,
};

extern bool zb_isDeviceJoinedNwk(void);
extern bool zdo_ifZdoNwkManagerIdle(void);
extern uint8_t zb_setPollRate(uint32_t newRate);
extern uint32_t zb_getPollRate(void);

static void app_bdb_activate_poll_rate(void)
{
#if APP_BDB_ROLE_ED
	uint32_t poll_rate = zb_getPollRate();

	if (poll_rate == 0U) {
		poll_rate = POLL_RATE;
	}

	(void)zb_setPollRate(poll_rate);
#endif
}

static void app_bdb_commissioning_retry_schedule(void);
static void app_bdb_commissioning_retry_cancel(void);
/* forward declaration so retry handler can call it */
void app_bdb_start_commissioning(void);

static int app_bdb_commissioning_retry(void *data)
{
	bool joined = zb_isDeviceJoinedNwk();
	bool idle = zdo_ifZdoNwkManagerIdle();

	ARG_UNUSED(data);
	commissioning_retry_timer = NULL;

	if (!joined && !search_open) {
		/* the window is closed (or was never opened): leave it, nothing to retry */
		commissioning_start_requested = false;
		leave_recommission_pending = false;
		return -1;
	}

	if (leave_recommission_pending) {
		if (!bdb_runtime_ready || joined || commissioning_start_requested) {
			app_bdb_commissioning_retry_schedule();
			return -1;
		}

		leave_recommission_pending = false;
		app_bdb_start_commissioning();
		if (!commissioning_start_requested) {
			leave_recommission_pending = true;
			app_bdb_commissioning_retry_schedule();
		}
		return -1;
	}

	if (joined) {
		app_bdb_activate_poll_rate();
		commissioning_start_requested = true;
#if APP_BDB_ROLE_ROUTER
		zb_router_enable_parenting(0xffU);
#endif
		return -1;
	}
	if (!idle) {
		commissioning_start_requested = true;
		app_bdb_commissioning_retry_schedule();
		return -1;
	}

	commissioning_start_requested = false;
	app_bdb_start_commissioning();
	return -1;
}

static void app_bdb_commissioning_retry_cancel(void)
{
	if (commissioning_retry_timer != NULL) {
		(void)ev_timer_taskCancel(&commissioning_retry_timer);
		commissioning_retry_timer = NULL;
	}
}

static void app_bdb_commissioning_retry_schedule(void)
{
	app_bdb_commissioning_retry_cancel();
	commissioning_retry_timer = ev_timer_taskPost(app_bdb_commissioning_retry, NULL,
						      APP_BDB_COMMISSIONING_RETRY_MS);
}
#endif /* CONFIG_ZIGBEE_BDB */

void zb_platform_app_runtime_reset(void)
{
#if defined(CONFIG_ZIGBEE_BDB)
	commissioning_start_requested = false;
	bdb_runtime_ready = false;
	leave_recommission_pending = false;
	commissioning_retry_timer = NULL;
	search_timer = NULL;
#endif
}

/* ------------------------------------------------------------------ */
/* Search window                                                       */
/* ------------------------------------------------------------------ */

#if defined(CONFIG_ZIGBEE_BDB)
static int search_window_end(void *data)
{
	ARG_UNUSED(data);
	search_timer = NULL;
	search_open = false;
	/* an attempt in progress ends by itself; nothing starts after it */
	return -1;
}

void zbr_search_start(uint32_t ms)
{
	if (search_timer != NULL) {
		(void)ev_timer_taskCancel(&search_timer);
		search_timer = NULL;
	}
	search_open = true;
	commissioning_start_requested = false;
	if (ms != 0U) {
		search_timer = ev_timer_taskPost(search_window_end, NULL, ms);
	}
	app_led_search_start();
	if (!zb_isDeviceJoinedNwk() && bdb_runtime_ready) {
		app_bdb_start_commissioning();
	}
}

void zbr_search_stop(void)
{
	if (search_timer != NULL) {
		(void)ev_timer_taskCancel(&search_timer);
		search_timer = NULL;
	}
	search_open = false;
}

bool zbr_search_active(void)
{
	return search_open;
}
#else
void zbr_search_start(uint32_t ms) { ARG_UNUSED(ms); }
void zbr_search_stop(void) {}
bool zbr_search_active(void) { return false; }
#endif

/* ------------------------------------------------------------------ */
/* Public API                                                          */
/* ------------------------------------------------------------------ */

bool app_bdb_get_join_profile(struct zb_platform_bdb_join_profile *profile)
{
#if !defined(CONFIG_ZIGBEE_BDB)
	ARG_UNUSED(profile);
	return false;
#else
	if (profile == NULL) {
		return false;
	}

	memset(profile, 0, sizeof(*profile));
	profile->channel_mask = (uint32_t)ZB_CHANNEL_SCAN_MASK;
	/*
	 * Real network joins must learn the NWK key during interview via the
	 * Trust Center Transport Key exchange.  Do not preload a test key
	 * here: it short-circuits interview and breaks live-network joins.
	 */
	profile->network_key_valid = false;
	if (APP_BDB_ROLE_ED) {
		memcpy(profile->tc_addr, app_bdb_fixed_tc_addr, sizeof(app_bdb_fixed_tc_addr));
		profile->tc_addr_valid = true;
	}

	return true;
#endif
}

void app_bdb_bootstrap_ready(void)
{
#if defined(CONFIG_ZIGBEE_BDB)
	if (bdb_runtime_ready && g_bdbCtx.simpleDesc == NULL) {
		/*
		 * The platform clears g_zbInfo before rebuilding the stack, but
		 * retained SRAM can preserve this application-level guard.
		 */
		bdb_runtime_ready = false;
		commissioning_start_requested = false;
		leave_recommission_pending = false;
		commissioning_retry_timer = NULL;
	}

	if (!bdb_runtime_ready) {
		int err = zb_platform_bdb_init_default();
		bool joined = zb_isDeviceJoinedNwk();
		bool idle = zdo_ifZdoNwkManagerIdle();

		if (err == 0) {
			bdb_runtime_ready = true;
#if defined(ZBR_SEARCH_ON_BOOT)
			/* no service button: the device has no other way to be paired */
			if (!joined) {
				search_open = true;
			}
#elif defined(ZBR_SEARCH_ON_POWER_ON)
			/*
			 * Power was applied (a plug, a battery), not a wake from sleep, and the device has no
			 * network: it searches for one, in a window. Once per boot: a leave ordered by the
			 * coordinator rebuilds the runtime and must not open another one.
			 */
			if (!power_on_checked) {
				power_on_checked = true;
				if (!joined && !search_open && zbr_chip_cold_boot()) {
					search_open = true;
					if (ZBR_SEARCH_MS != 0U) {
						search_timer = ev_timer_taskPost(search_window_end, NULL,
										 ZBR_SEARCH_MS);
					}
					app_led_search_start();
				}
			}
#endif
			if (joined && idle && APP_BDB_ROLE_ED) {
				app_bdb_activate_poll_rate();
			}
#if APP_BDB_ROLE_ROUTER
			/*
			 * A restored router is already an always-on FFD.  Do not send it
			 * through the ED persistent-rejoin path; announce the restored
			 * short address and keep RX open for the coordinator interview.
			 */
			if (joined && idle) {
				zb_router_enable_parenting(0xffU);
			}
#endif
		} else {
			LOG_ERR("zigbee_shell BDB runtime init failed (%d)", err);
		}
	}
#endif
}

bool app_bdb_should_start_commissioning(void)
{
#if defined(CONFIG_ZIGBEE_BDB)
	bool should_start;

	if (!bdb_runtime_ready) {
		return false;
	}
	if (commissioning_start_requested || !search_open) {
		return false;
	}

	should_start = !zb_isDeviceJoinedNwk();
	return should_start;
#else
	return false;
#endif
}

void app_bdb_start_commissioning(void)
{
#if defined(CONFIG_ZIGBEE_BDB)
	uint8_t status;

	if (!bdb_runtime_ready) {
		LOG_WRN("zigbee_shell commissioning start: BDB runtime not initialized");
		return;
	}

	if (commissioning_start_requested) {
		LOG_WRN("zigbee_shell commissioning start: already requested");
		return;
	}

	if (zb_isDeviceJoinedNwk()) {
		commissioning_start_requested = true;
		if (APP_BDB_ROLE_ED) {
			app_bdb_activate_poll_rate();
		}
		app_bdb_commissioning_retry_cancel();
		return;
	}

	if (!search_open) {
		return;
	}

#if defined(CONFIG_ZIGBEE_COORDINATOR)
	status = zb_platform_bdb_network_formation_start();
#else
	status = zb_platform_bdb_network_steer_start();
#endif
	if (status == 0U) {
		commissioning_start_requested = true;
		if (zb_isDeviceJoinedNwk()) {
			app_bdb_commissioning_retry_cancel();
		}
		if (!zb_isDeviceJoinedNwk()) {
			app_bdb_commissioning_retry_schedule();
		}
		/*
		 * Keep this path non-blocking: the Zigbee thread must return to the
		 * scheduler so the queued BDB start task can run immediately.
		 */
	} else {
		commissioning_start_requested = false;
		app_bdb_commissioning_retry_schedule();
	}
#endif
}

void app_bdb_network_left(void)
{
#if defined(CONFIG_ZIGBEE_BDB)
	commissioning_start_requested = false;
	app_bdb_commissioning_retry_cancel();
	/* The service action opens the window before it leaves; a leave ordered by the coordinator
	 * does not, and leaves the device waiting for its button. */
	leave_recommission_pending = search_open;
	if (search_open) {
		app_bdb_commissioning_retry_schedule();
	}
#endif
}

/*
 * Callback fired by the ZDO layer when the coordinator's Active_EP_rsp
 * arrives.  Drives the interview: issues SimpleDesc + Basic-cluster read
 * for each endpoint reported by the coordinator.
 *
 * `p` is a `zdo_zdpDataInd_t *` allocated by the ZDO layer with `zpdu`
 * pointing at the raw Active_EP_rsp payload (seq, status, nwk_addr,
 * ep_count, ep_list[]).  This is the same contract used by bdb.c callbacks.
 */
static void app_bdb_active_ep_rsp_cb(void *p)
{
	const zdo_zdpDataInd_t *ind = (const zdo_zdpDataInd_t *)p;
	const zdo_active_ep_resp_t *rsp;
	u8 i;

	if ((ind == NULL) || (ind->zpdu == NULL)) {
		return;
	}

	rsp = (const zdo_active_ep_resp_t *)ind->zpdu;
	if (ind->status != (u8)ZDO_SUCCESS || rsp->active_ep_count == 0U) {
		return;
	}

	for (i = 0U; i < rsp->active_ep_count && i < MAX_REQUESTED_CLUSTER_NUMBER; i++) {
		zdo_simple_descriptor_req_t sd_req;
		epInfo_t ep_info;
		struct {
			u8 numAttr;
			u16 attrID[2];
		} basic_read = {2U, {0x0004U, 0x0005U}};
		u8 seq = 0U;
		u8 ep = rsp->active_ep_lst[i];

		sd_req.nwk_addr_interest = ind->src_addr;
		sd_req.endpoint = ep;
		(void)zb_zdoSimpleDescReq(ind->src_addr, &sd_req, &seq, NULL);

		memset(&ep_info, 0, sizeof(ep_info));
		ep_info.dstAddr.shortAddr = ind->src_addr;
		ep_info.dstEp = ep;
		ep_info.profileId = APP_PROFILE_HA_PROFILE_ID;
		ep_info.dstAddrMode = APS_SHORT_DSTADDR_WITHEP;
		ep_info.txOptions = APS_TX_OPT_ACK_TX;
		ep_info.radius = 30U;
		seq = 0U;
		(void)zcl_read(APP_PROFILE_ENDPOINT, &ep_info, 0x0000U, 0U, 1U, 0U, seq,
			       (zclReadCmd_t *)&basic_read);
	}
}

void app_bdb_commissioning_status(uint8_t status, bool joinedNetwork)
{
#if defined(CONFIG_ZIGBEE_BDB)
	if (joinedNetwork) {
		zbr_search_stop();
		if (APP_BDB_ROLE_ED) {
			app_bdb_activate_poll_rate();
		}
		leave_recommission_pending = false;
		commissioning_start_requested = true;
		app_bdb_commissioning_retry_cancel();
		if (APP_BDB_ROLE_ED && status == 0x00U) {
			/*
			 * Post-join ZDO/ZCL interview: discover coordinator endpoints.
			 * The response callback app_bdb_active_ep_rsp_cb will issue
			 * SimpleDesc + Basic-cluster read for each discovered endpoint,
			 * making the interview flow response-driven rather than hardcoded.
			 */
			zdo_active_ep_req_t ep_req = {.nwk_addr_interest = 0x0000U};
			u8 seq = 0U;

			(void)zb_zdoActiveEpReq(0x0000U, &ep_req, &seq, app_bdb_active_ep_rsp_cb);
		}
#if APP_BDB_ROLE_ROUTER
		/*
		 * We are now in the network — open ourselves as a parent so
		 * other devices can join THROUGH this router.
		 */
		zb_router_enable_parenting(0xffU);
#endif
		LOG_INF("zigbee_shell joined network (bdb status: 0x%02x)", status);
		return;
	}

	if (zb_isDeviceJoinedNwk()) {
		zbr_search_stop();
		if (APP_BDB_ROLE_ED) {
			app_bdb_activate_poll_rate();
		}
		leave_recommission_pending = false;
		commissioning_start_requested = true;
		app_bdb_commissioning_retry_cancel();
#if APP_BDB_ROLE_ROUTER
		/* Joined (per state check) — open ourselves as a parent. */
		zb_router_enable_parenting(0xffU);
#endif
		LOG_INF("zigbee_shell joined network after status check (bdb status: 0x%02x)",
			status);
		return;
	}

	commissioning_start_requested = false;
	if (!search_open) {
		return;     /* the window closed while this attempt ran */
	}
	if (APP_BDB_ROLE_ROUTER) {
		LOG_INF("zigbee_shell router commissioning pending (bdb status: 0x%02x), retry "
			"scheduled",
			status);
	} else {
		LOG_INF("zigbee_shell not joined yet (bdb status: 0x%02x), retry scheduled",
			status);
	}
	app_bdb_commissioning_retry_schedule();
#else
	ARG_UNUSED(status);
	ARG_UNUSED(joinedNetwork);
#endif
}

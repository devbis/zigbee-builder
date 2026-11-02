/* SPDX-License-Identifier: Apache-2.0 */

#include "zbr_input.h"
#include "zb_gen.h"

#if defined(CONFIG_ZIGBEE_SLEEPY)
#include <zephyr/pm/policy.h>
#endif

#include "zb_common.h"
#include "zbapi/zb_api.h"

/* How long after an input event a sleepy device stays up: the report goes out, the
 * coordinator's answer (enroll response, a command) arrives in the fast poll window. */
#define AFTER_EVENT_MS 2000

#if defined(CONFIG_ZIGBEE_SLEEPY)
static struct k_work_delayable awake_release;
static bool awake_held;
static bool awake_init;

static void awake_release_fn(struct k_work *work)
{
	ARG_UNUSED(work);
	if (awake_held) {
		pm_policy_state_lock_put(PM_STATE_SOFT_OFF, PM_ALL_SUBSTATES);
		awake_held = false;
	}
}

void zbr_stay_awake(uint32_t ms)
{
	if (!awake_init) {
		k_work_init_delayable(&awake_release, awake_release_fn);
		awake_init = true;
	}
	if (!awake_held) {
		pm_policy_state_lock_get(PM_STATE_SOFT_OFF, PM_ALL_SUBSTATES);
		awake_held = true;
	}
	(void)k_work_reschedule(&awake_release, K_MSEC(ms));
}
#else
void zbr_stay_awake(uint32_t ms)
{
	ARG_UNUSED(ms);
}
#endif

static void input_task(void *arg)
{
	struct zbr_input *in = arg;

	if (in->cb != NULL) {
		in->cb(in->state > 0);
	}
	zbr_stay_awake(AFTER_EVENT_MS);
}

static void hold_task(void *arg)
{
	struct zbr_input *in = arg;

	if (in->on_hold != NULL) {
		in->on_hold();
	}
	zbr_stay_awake(AFTER_EVENT_MS);
}

static void input_held(struct k_work *work)
{
	struct zbr_input *in =
		CONTAINER_OF(k_work_delayable_from_work(work), struct zbr_input, hold_work);

	if (in->state > 0) {
		(void)tl_zbTaskPost(hold_task, in);
	}
}

static void input_debounced(struct k_work *work)
{
	struct zbr_input *in =
		CONTAINER_OF(k_work_delayable_from_work(work), struct zbr_input, work);
	int level = gpio_pin_get_dt(&in->spec);
	bool first = in->state < 0;

	if (level < 0 || level == in->state) {
		return;
	}
	in->state = (int8_t)level;

	if (in->hold_ms != 0U) {
		if (level > 0) {
			/* stay up for the whole hold: a sleepy device must not sleep under the finger */
			zbr_stay_awake((uint32_t)in->hold_ms + AFTER_EVENT_MS);
			(void)k_work_reschedule(&in->hold_work, K_MSEC(in->hold_ms));
		} else {
			(void)k_work_cancel_delayable(&in->hold_work);
		}
	}
	if (first && (in->initial == 2U || (in->initial == 1U && level == 0))) {
		return;
	}
	(void)tl_zbTaskPost(input_task, in);
}

static void input_isr(const struct device *port, struct gpio_callback *cb, uint32_t pins)
{
	struct zbr_input *in = CONTAINER_OF(cb, struct zbr_input, gcb);

	ARG_UNUSED(port);
	ARG_UNUSED(pins);
	zbr_stay_awake(in->debounce_ms + AFTER_EVENT_MS);
	(void)k_work_reschedule(&in->work, K_MSEC(in->debounce_ms));
}

void zbr_input_init(struct zbr_input *in, size_t n)
{
	for (size_t i = 0; i < n; i++, in++) {
		if (!gpio_is_ready_dt(&in->spec)) {
			continue;
		}
		in->state = -1;
		k_work_init_delayable(&in->work, input_debounced);
		k_work_init_delayable(&in->hold_work, input_held);
		(void)gpio_pin_configure_dt(&in->spec, GPIO_INPUT);
		gpio_init_callback(&in->gcb, input_isr, BIT(in->spec.pin));
		(void)gpio_add_callback(in->spec.port, &in->gcb);
		(void)gpio_pin_interrupt_configure_dt(&in->spec, GPIO_INT_EDGE_BOTH);
		if (in->wake) {
			zbr_chip_wake_arm(&in->spec);
		}
		/* The first read: after a wake by this pin the edge is gone, the level is not */
		(void)k_work_reschedule(&in->work, K_MSEC(in->debounce_ms));
		zbr_stay_awake(in->debounce_ms + AFTER_EVENT_MS);
	}
}

/* SPDX-License-Identifier: Apache-2.0 */
/*
 * GPIO inputs of a generated device: debounced, edge driven, able to end a deep
 * sleep. The callback runs on the Zigbee thread, where attributes may be set,
 * for every change of the logical level (and once after init with the level the
 * pin has then, so a wake by the pin reports it).
 */
#pragma once

#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>

struct zbr_input {
	struct gpio_dt_spec spec;
	uint16_t debounce_ms;
	bool wake;                  /* may end a deep sleep */
	void (*cb)(bool active);
	/* the first read after init: 0 always reported, 1 only when active (a button held at a
	 * wake by it), 2 never */
	uint8_t initial;
	/* held this long (0: not watched), on_hold runs once on the Zigbee thread */
	uint16_t hold_ms;
	void (*on_hold)(void);

	/* private */
	struct gpio_callback gcb;
	struct k_work_delayable work;
	struct k_work_delayable hold_work;
	int8_t state;               /* -1 until the first read */
};

void zbr_input_init(struct zbr_input *in, size_t n);

/* Keep a sleepy device awake for @p ms from now (extends a running hold) */
void zbr_stay_awake(uint32_t ms);

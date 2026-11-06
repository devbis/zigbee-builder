/* The wake of a deep sleep of the BL702: a reset by the RTC (the fork's hibernate has no pin wake) */
#pragma once

#include <stdbool.h>

#include <zephyr/drivers/gpio.h>

#if defined(CONFIG_ZIGBEE_SLEEPY_DEEP)
#include "zb_ed_sleep.h"

/* True when the power was applied; false when a hibernate ended */
static inline bool zbr_chip_cold_boot(void)
{
	return !zb_ed_sleep_woke_from_deep();
}
#else
static inline bool zbr_chip_cold_boot(void)
{
	return true;
}
#endif

static inline void zbr_chip_wake_arm(const struct gpio_dt_spec *spec)
{
	(void)spec;       /* nothing wakes the BL702 but its RTC */
}

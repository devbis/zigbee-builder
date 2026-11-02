/* Let a pin end a deep sleep (the wake is a reset; init then sees the level). */
#pragma once

#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/gpio.h>

#if defined(CONFIG_ZIGBEE_SLEEPY_DEEP)
#include "power.h"

static inline void zbr_chip_wake_arm(const struct gpio_dt_spec *spec)
{
	static const struct device *const ports[] = {
		DEVICE_DT_GET(DT_NODELABEL(gpioa)), DEVICE_DT_GET(DT_NODELABEL(gpiob)),
		DEVICE_DT_GET(DT_NODELABEL(gpioc)), DEVICE_DT_GET(DT_NODELABEL(gpiod)),
	};

	for (size_t i = 0; i < ARRAY_SIZE(ports); i++) {
		if (spec->port == ports[i]) {
			(void)tlsr8258_pm_configure_gpio_wakeup(
				(uint8_t)i, spec->pin, (spec->dt_flags & GPIO_ACTIVE_LOW) != 0U, true);
			return;
		}
	}
}
#else
static inline void zbr_chip_wake_arm(const struct gpio_dt_spec *spec)
{
	ARG_UNUSED(spec);
}
#endif

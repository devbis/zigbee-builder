/*
 * Plugin of examples/custom-cluster.yaml: a manufacturer specific cluster 0xFC11 with a mode,
 * a temperature offset (both kept in NV) and a counter the plugin updates. Command 0 resets the
 * counter. The generated zb_user.h has everything the plugin may call or implement.
 */
#include "zb_user.h"

static uint32_t ticks;

void zb_user_init(void)
{
	/* a default the coordinator can change; persisted attributes are restored before this */
	if (zb_heater_get_mode() > 2) {
		zb_heater_set_mode(0);
	}
}

void zb_user_measure(void)
{
	zb_heater_set_runtime_s(++ticks);
}

void zb_heater_on_write(void)
{
	/* mode or offset changed by the coordinator: apply it to the hardware here */
}

uint8_t zb_heater_on_command(uint8_t cmd, const uint8_t *data, uint16_t len)
{
	(void)data;
	(void)len;
	if (cmd == ZB_HEATER_CMD_RESET) {
		ticks = 0;
		zb_heater_set_runtime_s(0);
		return 0;
	}
	return 0x81; /* unsupported cluster command */
}

/*
 * Plugin of examples/intercom-auto.yaml: the "open always" mode of an intercom tap in its plainest form.
 * When a call starts (the line input goes active) the handset's speaker is muted, and one second later the door
 * strike is pressed (a pulse relay: it turns itself off). If the caller hangs up first, nothing opens.
 *
 * What it uses is generated into zb_user.h from the YAML's component ids:
 *   zb_input_line_on_change(active)   the input 'line' changed (after debounce and release delay)
 *   zb_relay_door_set(on), zb_relay_mute_set(on)    switch a relay: its attribute and pulse follow
 *   zb_after(ms, fn, arg), zb_cancel(handle)        one-shot timers on the Zigbee thread
 */
#include "zb_user.h"

#define OPEN_AFTER_MS 1000

static int pending = -1;

static void open_door(void *arg)
{
	(void)arg;
	pending = -1;
	zb_relay_door_set(true);
}

void zb_input_line_on_change(bool active)
{
	zb_relay_mute_set(active);
	if (active) {
		if (pending < 0) {
			pending = zb_after(OPEN_AFTER_MS, open_door, NULL);
		}
	} else if (pending >= 0) {
		(void)zb_cancel(pending);
		pending = -1;
	}
}

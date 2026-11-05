/* SPDX-License-Identifier: Apache-2.0 */
/*
 * One-shot timers for plugins, on the Zigbee thread (the callback may set attributes and call the
 * generated zb_relay_* functions). Eight at a time.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* Run fn(arg) once after ms (at least 1). Returns a handle, or -1 when all eight are in use. */
int zb_after(uint32_t ms, void (*fn)(void *arg), void *arg);

/* Cancel a timer that has not fired. True when it was still pending; a handle that has fired is refused. */
bool zb_cancel(int handle);

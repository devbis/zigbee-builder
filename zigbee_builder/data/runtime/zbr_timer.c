/* SPDX-License-Identifier: Apache-2.0 */

#include "zbr_timer.h"

#include "zb_common.h"
#include "os/ev_timer.h"

#define SLOTS 8U

struct slot {
	void (*fn)(void *arg);
	void *arg;
	ev_timer_event_t *evt;
	uint16_t gen;
	bool used;
};

static struct slot slots[SLOTS];

/* a handle is the slot and how many times it was used, so a stale one cancels nothing */
static int handle_of(unsigned int i)
{
	return (int)(((unsigned int)slots[i].gen << 4) | i);
}

static int fire(void *arg)
{
	struct slot *s = arg;
	void (*fn)(void *) = s->fn;
	void *a = s->arg;

	s->used = false;
	s->evt = NULL;
	fn(a);
	return -1;                    /* once */
}

int zb_after(uint32_t ms, void (*fn)(void *arg), void *arg)
{
	if (fn == NULL) {
		return -1;
	}
	for (unsigned int i = 0; i < SLOTS; i++) {
		if (!slots[i].used) {
			slots[i].used = true;
			slots[i].fn = fn;
			slots[i].arg = arg;
			slots[i].gen++;
			slots[i].evt = ev_timer_taskPost(fire, &slots[i], ms == 0U ? 1U : ms);
			if (slots[i].evt == NULL) {
				slots[i].used = false;
				return -1;
			}
			return handle_of(i);
		}
	}
	return -1;
}

bool zb_cancel(int handle)
{
	unsigned int i = (unsigned int)handle & 0xFU;

	if (handle < 0 || i >= SLOTS || !slots[i].used ||
	    slots[i].gen != (uint16_t)((unsigned int)handle >> 4)) {
		return false;
	}
	(void)ev_timer_taskCancel(&slots[i].evt);
	slots[i].used = false;
	slots[i].evt = NULL;
	return true;
}

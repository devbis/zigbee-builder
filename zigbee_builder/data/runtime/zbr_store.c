/* SPDX-License-Identifier: Apache-2.0 */

#include <string.h>

#include "zbr_store.h"
#include "drv_nv.h"

#define VERSION 1U

struct rec {
	uint8_t version;
	uint8_t data[ZBR_STORE_MAX];
	uint8_t crc;
};

static uint8_t crc8(const uint8_t *p, size_t n)
{
	uint8_t crc = 0U;

	while (n-- > 0U) {
		crc ^= *p++;
		for (int i = 0; i < 8; i++) {
			crc = (crc & 0x80U) ? (uint8_t)((crc << 1) ^ 0x07U) : (uint8_t)(crc << 1);
		}
	}
	return crc;
}

bool zbr_store_load(uint8_t item, void *buf, size_t n)
{
	struct rec r;

	if (n > ZBR_STORE_MAX ||
	    nv_flashReadNew(1, NV_MODULE_APP, item, sizeof(r), (uint8_t *)&r) != NV_SUCC ||
	    r.version != VERSION || r.crc != crc8((const uint8_t *)&r, offsetof(struct rec, crc))) {
		return false;
	}
	memcpy(buf, r.data, n);
	return true;
}

void zbr_store_save(uint8_t item, const void *buf, size_t n)
{
	struct rec r;

	if (n > ZBR_STORE_MAX) {
		return;
	}
	memset(&r, 0, sizeof(r));
	r.version = VERSION;
	memcpy(r.data, buf, n);
	r.crc = crc8((const uint8_t *)&r, offsetof(struct rec, crc));
	(void)nv_flashWriteNew(1, NV_MODULE_APP, item, sizeof(r), (uint8_t *)&r);
}

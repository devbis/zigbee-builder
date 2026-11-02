/* SPDX-License-Identifier: Apache-2.0 */

#include <string.h>

#include "zbr_store.h"
#include "drv_nv.h"

#define VERSION 1U

struct rec {
	u8 version;
	u8 data[ZBR_STORE_MAX];
	u8 crc;
};

static u8 crc8(const u8 *p, size_t n)
{
	u8 crc = 0U;

	while (n-- > 0U) {
		crc ^= *p++;
		for (int i = 0; i < 8; i++) {
			crc = (crc & 0x80U) ? (u8)((crc << 1) ^ 0x07U) : (u8)(crc << 1);
		}
	}
	return crc;
}

bool zbr_store_load(u8 item, void *buf, size_t n)
{
	struct rec r;

	if (n > ZBR_STORE_MAX ||
	    nv_flashReadNew(1, NV_MODULE_APP, item, sizeof(r), (u8 *)&r) != NV_SUCC ||
	    r.version != VERSION || r.crc != crc8((const u8 *)&r, offsetof(struct rec, crc))) {
		return false;
	}
	memcpy(buf, r.data, n);
	return true;
}

void zbr_store_save(u8 item, const void *buf, size_t n)
{
	struct rec r;

	if (n > ZBR_STORE_MAX) {
		return;
	}
	memset(&r, 0, sizeof(r));
	r.version = VERSION;
	memcpy(r.data, buf, n);
	r.crc = crc8((const u8 *)&r, offsetof(struct rec, crc));
	(void)nv_flashWriteNew(1, NV_MODULE_APP, item, sizeof(r), (u8 *)&r);
}

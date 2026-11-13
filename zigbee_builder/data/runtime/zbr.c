/* SPDX-License-Identifier: Apache-2.0 */

#include <zephyr/kernel.h>

#include <string.h>

#include "zbr.h"
#include "zb_gen.h"
#include "zb_common.h"
#include "zbapi/zb_api.h"
#if defined(CONFIG_ZIGBEE_OTA)
#include "ota/ota.h"
#endif

#if defined(CONFIG_ZIGBEE_SLEEPY_DEEP)

static int32_t item_value(const struct zbr_item *it)
{
	switch (it->size) {
	case 1:
		return it->is_signed ? (int32_t)*(const int8_t *)it->val : (int32_t)*(const uint8_t *)it->val;
	case 2:
		return it->is_signed ? (int32_t)*(const int16_t *)it->val : (int32_t)*(const uint16_t *)it->val;
	default:
		return *(const int32_t *)it->val;
	}
}

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

static void report_group(uint8_t ep, uint16_t cluster, const struct zbr_item *first, const bool *due, uint8_t n)
{
	struct {
		uint8_t numAttr;
		zclReport_t attr[ZBR_MAX_GROUP];
	} report = {.numAttr = 0};
	epInfo_t dst;

	for (uint8_t i = 0; i < n && report.numAttr < ZBR_MAX_GROUP; i++) {
		if (due[i]) {
			report.attr[report.numAttr++] = (zclReport_t){
				first[i].attr, first[i].type, (uint8_t *)first[i].val};
		}
	}
	if (report.numAttr == 0U || !zb_bindingTblSearched(cluster, ep)) {
		return;
	}

	memset(&dst, 0, sizeof(dst));
	dst.dstAddrMode = APS_DSTADDR_EP_NOTPRESETNT;
	dst.profileId = APP_HA_PROFILE_ID;
	(void)zcl_sendReportAttrsCmd(ep, &dst, TRUE, ZCL_FRAME_SERVER_CLIENT_DIR, cluster,
				     (zclReportCmd_t *)&report);
}

#if defined(ZBR_HAVE_RETAINED)
#define RET_MAGIC 0x5A425231U

static struct {
	uint32_t magic;
	uint32_t elapsed[ZBR_MAX_ITEMS]; /* seconds since the last report of each item */
	int32_t last[ZBR_MAX_ITEMS];    /* the value last reported */
	uint8_t crc;
} ret ZBR_RETAINED;

static uint8_t ret_crc(void)
{
	return crc8((const uint8_t *)&ret, offsetof(__typeof__(ret), crc));
}

static bool ret_load(void)
{
	if (ret.magic == RET_MAGIC && ret.crc == ret_crc()) {
		return true;
	}
	memset(&ret, 0, sizeof(ret));
	ret.magic = RET_MAGIC;
	return false;
}
#endif

static bool item_due(const struct zbr_item *it, bool fresh, uint32_t elapsed, int32_t last)
{
	const reportCfgInfo_t *e = zcl_reportCfgInfoEntryFind(it->ep, it->cluster, it->attr);
	int32_t v = item_value(it);
	uint32_t moved;

	if (e == NULL || e->maxInterval == 0xFFFFU || v == it->invalid) {
		return false;
	}
	if (fresh || (e->maxInterval != 0U && elapsed >= e->maxInterval)) {
		return true;
	}
	moved = (uint32_t)(v > last ? v - last : last - v);
	return elapsed >= e->minInterval && moved > 0U &&
	       moved >= BUILD_U16(e->reportableChange[0], e->reportableChange[1]);
}

void zbr_report_all(const struct zbr_item *items, size_t n, uint32_t slept_s)
{
	bool due[ZBR_MAX_ITEMS] = {0};
#if defined(ZBR_HAVE_RETAINED)
	bool fresh = !ret_load();

	for (size_t i = 0; i < n; i++) {
		ret.elapsed[i] = MIN(ret.elapsed[i] + slept_s, 0xFFFFU);
		due[i] = item_due(&items[i], fresh, ret.elapsed[i], ret.last[i]);
	}
#else
	for (size_t i = 0; i < n; i++) {
		due[i] = items[i].invalid != item_value(&items[i]) &&
			 zcl_reportCfgInfoEntryFind(items[i].ep, items[i].cluster, items[i].attr) != NULL;
	}
	ARG_UNUSED(slept_s);
#endif

	for (size_t i = 0; i < n;) {
		size_t j = i;

		while (j < n && items[j].ep == items[i].ep && items[j].cluster == items[i].cluster) {
			j++;
		}
		report_group(items[i].ep, items[i].cluster, &items[i], &due[i], (uint8_t)(j - i));
		i = j;
	}

#if defined(ZBR_HAVE_RETAINED)
	for (size_t i = 0; i < n; i++) {
		if (due[i]) {
			ret.elapsed[i] = 0U;
			ret.last[i] = item_value(&items[i]);
		}
	}
	ret.crc = ret_crc();
#endif
}

#else /* not deep sleeping: the stack's own reporting table does the work */

void zbr_report_all(const struct zbr_item *items, size_t n, uint32_t slept_s)
{
	ARG_UNUSED(items);
	ARG_UNUSED(n);
	ARG_UNUSED(slept_s);
	report_handler();
}

#endif

#if defined(CONFIG_ZIGBEE_OTA)
static ota_preamble_t ota_preamble = {
	.fileVer = APP_OTA_FILE_VERSION,
	.totalImageSize = 0U,
	.imageType = APP_OTA_IMAGE_TYPE,
	.manufacturerCode = CONFIG_ZIGBEE_MANUFACTURER_CODE,
};

static void ota_event(uint8_t evt, uint8_t status)
{
	if (evt == OTA_EVT_COMPLETE && status == ZCL_STA_SUCCESS) {
		/* MCUboot verifies and starts the image in slot 1 */
		ota_mcuReboot();
	}
}

static ota_callBack_t ota_cb = {
	.processMsgCbFunc = ota_event,
};
#endif

void zbr_ota_start(af_simple_descriptor_t *desc, bool periodic_query)
{
#if defined(CONFIG_ZIGBEE_OTA)
	ota_init(OTA_TYPE_CLIENT, desc, &ota_preamble, &ota_cb);
	if (periodic_query) {
		ota_queryStart(OTA_PERIODIC_QUERY_INTERVAL);
	}
#else
	ARG_UNUSED(desc);
	ARG_UNUSED(periodic_query);
#endif
}

static status_t no_commands(zclIncoming_t *in)
{
	ARG_UNUSED(in);
	return ZCL_STA_UNSUP_CLUSTER_COMMAND;
}

status_t zbr_register_analog_input(uint8_t ep, uint16_t manu_code, uint8_t n_attrs, const zclAttrInfo_t *attrs,
				   cluster_forAppCb_t cb)
{
	return zcl_registerCluster(ep, ZCL_CLUSTER_GEN_ANALOG_INPUT_BASIC, manu_code, n_attrs, attrs,
				   no_commands, cb);
}

int32_t zbr_dp_scale(int32_t raw, int32_t mul, int32_t den)
{
	int32_t q = raw / den;
	int32_t r = raw % den;
	int32_t hi = INT32_MAX / mul;

	if (q > hi) {
		return INT32_MAX;
	}
	if (q < -hi) {
		return -INT32_MAX;
	}
	return q * mul + (r * mul) / den;
}

int32_t zbr_dp_unscale(int32_t v, int32_t mul, int32_t den)
{
	int32_t q = v / den;
	int32_t r = (v % den) * mul;
	int32_t hi = (INT32_MAX - mul) / mul;

	if (q > hi) {
		return INT32_MAX;
	}
	if (q < -hi) {
		return -INT32_MAX;
	}
	return q * mul + (r >= 0 ? (r + den / 2) / den : (r - den / 2) / den);
}

#define ZBR_CONCENTRATION(name, cluster)                                                                \
	status_t zbr_register_##name(uint8_t ep, uint16_t manu_code, uint8_t n_attrs, const zclAttrInfo_t *attrs,       \
				     cluster_forAppCb_t cb)                                             \
	{                                                                                                \
		return zcl_registerCluster(ep, (cluster), manu_code, n_attrs, attrs, no_commands, cb);   \
	}

ZBR_CONCENTRATION(co2, 0x040DU)
ZBR_CONCENTRATION(pm25, 0x042AU)
ZBR_CONCENTRATION(formaldehyde, 0x042BU)

void zbr_send_cluster_cmd(uint8_t ep, uint16_t cluster, uint16_t manu_code, uint8_t cmd, const uint8_t *data, uint16_t len)
{
	epInfo_t dst;

	if (!zb_isDeviceJoinedNwk() || !zb_bindingTblSearched(cluster, ep)) {
		return;
	}
	memset(&dst, 0, sizeof(dst));
	dst.dstAddrMode = APS_DSTADDR_EP_NOTPRESETNT;
	dst.profileId = APP_HA_PROFILE_ID;
	(void)zcl_sendCmd(ep, &dst, cluster, cmd, TRUE, ZCL_FRAME_SERVER_CLIENT_DIR, TRUE, manu_code,
			  ZCL_SEQ_NUM, len, (uint8_t *)data);
}

uint32_t zbr_float_bits(int32_t num, uint32_t den)
{
	uint32_t sign = 0U;
	uint32_t a, ip, rem, m;
	int32_t exp = 0;
	int nbits = 0;

	if (num == 0 || den == 0U) {
		return 0U;
	}
	if (num < 0) {
		sign = 0x80000000U;
		a = (uint32_t)(-(num + 1)) + 1U;
	} else {
		a = (uint32_t)num;
	}
	ip = a / den;
	rem = a % den;

	if (ip > 0U) {
		for (uint32_t t = ip; t != 0U; t >>= 1) {
			nbits++;
		}
		exp = nbits - 1;
		if (nbits > 24) {
			m = ip >> (nbits - 24);
		} else {
			m = ip;
			for (int i = nbits; i < 24; i++) {
				rem <<= 1;
				m <<= 1;
				if (rem >= den) {
					rem -= den;
					m |= 1U;
				}
			}
		}
	} else {
		/* below 1: find the first 1 bit of the fraction */
		exp = 0;
		do {
			rem <<= 1;
			exp--;
		} while (rem < den);
		rem -= den;
		m = 1U;
		for (int i = 1; i < 24; i++) {
			rem <<= 1;
			m <<= 1;
			if (rem >= den) {
				rem -= den;
				m |= 1U;
			}
		}
	}
	return sign | ((uint32_t)(exp + 127) << 23) | (m & 0x7FFFFFU);
}

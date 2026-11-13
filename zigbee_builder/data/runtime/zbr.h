/* SPDX-License-Identifier: Apache-2.0 */
/*
 * Table driven runtime of the generated firmware: the report decision of a
 * deep-sleeping end device, and the OTA client set-up. Everything device
 * specific is in the tables zb_device.c passes in.
 */
#pragma once

#include "app_common.h"

struct zbr_item {
	uint8_t ep;
	uint16_t cluster;
	uint16_t attr;
	uint8_t type;        /* ZCL_DATA_TYPE_* */
	const void *val;
	uint8_t size;        /* 1, 2 or 4 */
	bool is_signed;
	int32_t invalid;    /* the value of "not measured"; never reported */
};

/*
 * One wake of a deep-sleeping device: report what the coordinator's reporting
 * configuration (minimum/maximum interval, reportable change) makes due. The
 * time since each report and the value last reported live in retained RAM when
 * the chip has it; without it everything bound is reported on every wake.
 * @p slept_s is the length of the sleep that just ended.
 * Items of one cluster must be adjacent: they go out in one command.
 */
void zbr_report_all(const struct zbr_item *items, size_t n, uint32_t slept_s);

/* Start the OTA client on @p desc; @p periodic_query for devices that stay awake */
void zbr_ota_start(af_simple_descriptor_t *desc, bool periodic_query);

/* Cluster the stack has no code for (Analog Input ...): a table of attributes, no commands */
status_t zbr_register_analog_input(uint8_t ep, uint16_t manu_code, uint8_t n_attrs, const zclAttrInfo_t *attrs,
				   cluster_forAppCb_t cb);

/* A datapoint value times mul / den (the channel's milli-units), saturating, 32-bit arithmetic only;
 * mul * den < 2^31 */
int32_t zbr_dp_scale(int32_t raw, int32_t mul, int32_t den);

/* An attribute value times mul / den as a datapoint value (the inverse of the above), rounded to the nearest */
int32_t zbr_dp_unscale(int32_t v, int32_t mul, int32_t den);

/* Concentration measurement clusters (CO2 0x040D, PM2.5 0x042A, formaldehyde 0x042B), no commands */
status_t zbr_register_co2(uint8_t ep, uint16_t manu_code, uint8_t n_attrs, const zclAttrInfo_t *attrs, cluster_forAppCb_t cb);
status_t zbr_register_pm25(uint8_t ep, uint16_t manu_code, uint8_t n_attrs, const zclAttrInfo_t *attrs, cluster_forAppCb_t cb);
status_t zbr_register_formaldehyde(uint8_t ep, uint16_t manu_code, uint8_t n_attrs, const zclAttrInfo_t *attrs,
				   cluster_forAppCb_t cb);

/* Send a cluster specific command from @p ep to its bindings (server -> client) */
void zbr_send_cluster_cmd(uint8_t ep, uint16_t cluster, uint16_t manu_code, uint8_t cmd, const uint8_t *data, uint16_t len);

/*
 * IEEE 754 single precision bits of num/den, by integer arithmetic only (the TC32 toolchain has no
 * soft-float). Truncates; den > 0. A ZCL single precision attribute is stored as these 32 bits.
 */
uint32_t zbr_float_bits(int32_t num, uint32_t den);

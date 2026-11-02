/* SPDX-License-Identifier: Apache-2.0 */
/* Small blobs in the application's NV area, with a version byte and a CRC. */
#pragma once

#include "app_common.h"

#define ZBR_STORE_MAX 32U

/* False when nothing valid is stored under @p item (@p buf is left alone) */
bool zbr_store_load(u8 item, void *buf, size_t n);
/* NVS skips a write of what it already holds */
void zbr_store_save(u8 item, const void *buf, size_t n);

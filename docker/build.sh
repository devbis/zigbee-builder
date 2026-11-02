#!/bin/sh
# Build the image with the revisions of docker/pins.env.
#   docker/build.sh [tag]            (default tag: zigbee-builder)
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
tag=${1:-zigbee-builder}
# shellcheck disable=SC1091
. "$here/docker/pins.env"
exec docker build -f "$here/docker/Dockerfile" -t "$tag" \
    --build-arg ZEPHYR_REPO="$ZEPHYR_REPO" --build-arg ZEPHYR_REV="${ZEPHYR_REV%% *}" \
    --build-arg IOT_DRIVERS_REPO="$IOT_DRIVERS_REPO" --build-arg IOT_DRIVERS_REV="${IOT_DRIVERS_REV%% *}" \
    --build-arg TC32_LLVM_TAG="$TC32_LLVM_TAG" --build-arg TC32_LLVM_SHA256_ARM64="$TC32_LLVM_SHA256_ARM64" \
    --build-arg TC32_LLVM_SHA256_AMD64="$TC32_LLVM_SHA256_AMD64" \
    --build-arg MCUBOOT_REPO="$MCUBOOT_REPO" --build-arg MCUBOOT_REV="$MCUBOOT_REV" \
    --build-arg ZEPHYR_SDK_VERSION="$ZEPHYR_SDK_VERSION" --build-arg WEST_PROJECTS="$WEST_PROJECTS" \
    "$here"

# syntax=docker/dockerfile:1
#
# zigbee-builder with everything it builds with: the Zephyr fork and its modules, MCUboot, the TC32 LLVM
# (a binary release), the Zephyr SDK toolchains for ARM and RISC-V. Nothing is compiled here. Build it with docker/build.sh (it passes the pins).
#
#   docker run --rm -v "$PWD":/work zigbee-builder build device.yaml -o out
#
ARG UBUNTU=ubuntu:24.04

# ---------------------------------------------------------------------------------------------
# The TC32 LLVM: clang and lld of the fork for the TC32 target, from the fork's binary release.
# A stage of its own: the download is cached and the rest of the image can change without it.
# ---------------------------------------------------------------------------------------------
FROM ${UBUNTU} AS llvm-tc32
ARG TC32_LLVM_TAG
ARG TC32_LLVM_SHA256_ARM64
ARG TC32_LLVM_SHA256_AMD64
RUN test -n "$TC32_LLVM_TAG" || { echo "pass the pins: use docker/build.sh" >&2; exit 1; }
RUN apt-get update && apt-get install -y --no-install-recommends wget ca-certificates && rm -rf /var/lib/apt/lists/*
RUN set -eux;     case "$(uname -m)" in       aarch64) a=arm64; sum="$TC32_LLVM_SHA256_ARM64" ;;       x86_64)  a=amd64; sum="$TC32_LLVM_SHA256_AMD64" ;;       *) echo "no TC32 LLVM for $(uname -m)" >&2; exit 1 ;;     esac;     wget -q "https://github.com/modern-tc32/llvm-project/releases/download/${TC32_LLVM_TAG}/tc32-llvm-toolchain-linux-${a}.tar.gz" -O /tmp/llvm.tar.gz;     echo "${sum}  /tmp/llvm.tar.gz" | sha256sum -c -;     mkdir /opt/tc32-llvm; tar xzf /tmp/llvm.tar.gz -C /opt/tc32-llvm; rm /tmp/llvm.tar.gz
# the release has llvm-readobj but not the llvm-readelf name Zephyr looks for (the tool works by the name it is run as)
RUN ln -s llvm-readobj /opt/tc32-llvm/bin/llvm-readelf
# the tools the builds use: say which is missing
RUN /opt/tc32-llvm/bin/clang --version; \
    for t in clang ld.lld llvm-ar llvm-nm llvm-objcopy llvm-objdump llvm-ranlib llvm-readelf; do \
      test -x /opt/tc32-llvm/bin/$t || { echo "MISSING: $t" >&2; miss=1; }; done; test -z "${miss:-}"

# ---------------------------------------------------------------------------------------------
FROM ${UBUNTU} AS final
ARG ZEPHYR_REPO
ARG ZEPHYR_REV
ARG IOT_DRIVERS_REPO
ARG IOT_DRIVERS_REV
ARG MCUBOOT_REPO
ARG MCUBOOT_REV
ARG ZEPHYR_SDK_VERSION
ARG WEST_PROJECTS
RUN test -n "$MCUBOOT_REV" &&  test -n "$ZEPHYR_REV" && test -n "$IOT_DRIVERS_REV" && test -n "$WEST_PROJECTS" \
    || { echo "pass the pins: use docker/build.sh" >&2; exit 1; }

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates wget xz-utils file make gcc g++ cmake ninja-build ccache \
        device-tree-compiler gperf python3 python3-venv python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Zephyr SDK: the ARM (nRF52) and RISC-V (ESP32) toolchains. Not the whole SDK.
RUN set -eux; arch="$(uname -m)"; \
    wget -q "https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v${ZEPHYR_SDK_VERSION}/zephyr-sdk-${ZEPHYR_SDK_VERSION}_linux-${arch}_minimal.tar.xz" -O /tmp/sdk.tar.xz; \
    mkdir /opt/zephyr-sdk; tar xf /tmp/sdk.tar.xz --strip-components=1 -C /opt/zephyr-sdk; rm /tmp/sdk.tar.xz; \
    /opt/zephyr-sdk/setup.sh -t arm-zephyr-eabi -t riscv64-zephyr-elf

# west and what the Zephyr fork and MCUboot need to run
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -U pip wheel west esptool
ENV PATH=/opt/venv/bin:$PATH

# The workspace: the fork at its revision, then only the modules these chips use, at the manifest's revisions
RUN set -eux; mkdir /opt/ws && cd /opt/ws; \
    git init -q zephyr && cd zephyr && git remote add origin "$ZEPHYR_REPO" \
    && git fetch -q --depth 1 origin "$ZEPHYR_REV" && git checkout -q FETCH_HEAD && cd ..; \
    west init -l zephyr; \
    west update --narrow -o=--depth=1 $WEST_PROJECTS
# the manifest names a branch for MCUboot: take the pinned commit (a west project's git remote is not called origin)
RUN cd /opt/ws/bootloader/mcuboot && git fetch -q --depth 1 "$MCUBOOT_REPO" "$MCUBOOT_REV" && git checkout -q FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "$MCUBOOT_REV"
# west's own needs (blobs fetching), then those of Zephyr's scripts and of imgtool
RUN pip install --no-cache-dir -r /opt/ws/zephyr/scripts/requirements-base.txt \
    && pip install --no-cache-dir -r /opt/ws/bootloader/mcuboot/scripts/requirements.txt
RUN cd /opt/ws && west blobs fetch hal_espressif

# Drivers of the devices (energy meters, displays ...): a Zephyr module the builds add on their own
RUN git init -q /opt/ws/zephyr-iot-drivers && cd /opt/ws/zephyr-iot-drivers && git remote add origin "$IOT_DRIVERS_REPO" \
    && git fetch -q --depth 1 origin "$IOT_DRIVERS_REV" && git checkout -q FETCH_HEAD

COPY --from=llvm-tc32 /opt/tc32-llvm /opt/tc32-llvm

# The builder
COPY pyproject.toml /src/pyproject.toml
COPY zigbee_builder /src/zigbee_builder
RUN pip install --no-cache-dir /src

# What zigbee-builder reads (see README, "Chips")
ENV ZEPHYR_BASE=/opt/ws/zephyr \
    ZEPHYR_EXTRA_MODULES=/opt/ws/zephyr-iot-drivers \
    MCUBOOT_DIR=/opt/ws/bootloader/mcuboot \
    LLVM_TOOLCHAIN_PATH=/opt/tc32-llvm \
    ZEPHYR_SDK_INSTALL_DIR=/opt/zephyr-sdk \
    WEST=/opt/venv/bin/west \
    PYTHON=/opt/venv/bin/python \
    HOME=/tmp

WORKDIR /work
ENTRYPOINT ["zigbee-builder"]
CMD ["--help"]

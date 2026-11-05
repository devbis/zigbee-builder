# zigbee-devices-builder

ESPHome-like builder: a YAML description of a Zigbee device → a Zephyr project → signed
MCUboot image, OTA file (`.zigbee`) and a full flash image. Chip, pins and role are data;
the device code is generated from a cluster catalog plus a small C runtime.

    python -m venv .venv && .venv/bin/pip install -e '.[dev]'
    zigbee-builder validate examples/temp-humidity-sht30.yaml
    zigbee-builder generate examples/temp-humidity-sht30.yaml -o build
    zigbee-builder build examples/temp-humidity-sht30.yaml -o build \
        --env-file ../zigbee-devices-firmware/scripts/env.local   # ZEPHYR_BASE, LLVM_TOOLCHAIN_PATH, ...

Changing a board = editing `pins:`; changing the chip = `zigbee.chip:` (plus pins in that chip's names).
A device file is for one board: the chip and the pins are in it, one name per pin.
For tests and checks the same device file can be built on another board, `--board` giving the chip and the
pins of that board (they replace the file's own `pins:` as a whole; `button` and `status_led` stay):

    zigbee-builder build examples/plug-relay.yaml --board examples/boards/esp32h2/plug-relay.yaml

## Joining and resetting: the service buttons

A device out of the network does not search for one by itself, and a device in a network never does. The search
starts when a *service input* is held (3 to 10 s, default 5), and runs in a window (default 60 s); it ends when the
device joins or the window closes, and a sleepy device then sleeps. Held in the network, a service input leaves it,
erases the NV (the network, and with it everything the firmware kept: relay state, energy, IAS enrollment, custom
attributes; `zb_user_network_left()` lets a plugin reset what it keeps in RAM) and opens the window.

    button: pair                    # one service button: a pin of pins:

    service:                        # or several, any of them
      hold: 5s
      search_time: 60s
      inputs: any                   # any binary_input; or [k1, k4]; or [rst], a recessed button of its own

When the power is applied (plugged in, a battery inserted) and the device has no network, it searches for one in
the same window, with no button; a wake from sleep does not (`service: {search_on_power_on: false}` turns this off).
`button:` and `service:` may be given together (the button is one more service input). A device with neither has
no way to be paired by hand and searches from boot until it joins. A service input is an ordinary `binary_input`:
its short press still does what its endpoint or relay says (the action is taken on the press, so a key that is held
for a reset acts once at the start). See `examples/wall-switch-4.yaml`. Not done: the sleep of a device out of the
network (the stack refuses it), which needs a change in the Zephyr fork.

## Docker

The image has everything the builds need (the Zephyr fork and its modules, MCUboot, the TC32 LLVM from the fork's
binary release, the Zephyr SDK toolchains for ARM and RISC-V, the drivers, this builder); nothing is compiled while it
is built. The revisions are in `docker/pins.env`.

    docker/build.sh                 # builds zigbee-builder (about 20 minutes, 5 GB; the first time)
    docker run --rm -v "$PWD":/work zigbee-builder build examples/plug-relay.yaml -o build
    docker run --rm -v "$PWD":/work zigbee-builder build examples/plug-relay.yaml \
        --board examples/boards/nrf52840/plug-relay.yaml -o build

The same image builds for the chips below (a build takes about a minute in it). It is built for the machine's
architecture (arm64 or amd64; the TC32 LLVM release has both, checked with its SHA-256). Checked: one build per
chip on arm64.

## Clusters: multiplier and divisor

Measured attributes of Electrical Measurement and Simple Metering come with their `multiplier` and `divisor`
attributes, which are derived: the catalog (`data/clusters`) has the defaults (voltage 1/100, current 1/1000,
power 1/1, energy 1/1000) and a binding may override them for one quantity. The pair is read-only, it is in the
cluster only when its value is bound, and the value's step (`multiplier / divisor` of the unit) follows it:

    electrical_measurement:
      source: meter                                  # every quantity from the meter
      rms_current: {source: meter, multiplier: 1, divisor: 100}    # the current in 10 mA

Concentration clusters the stack has no code for: `co2` (0x040D, the channel in ppm, the attribute a fraction),
`formaldehyde` (0x042B) and `pm25` (0x042A, ug/m3).

## A host MCU on a UART (Tuya)

`type: tuya_mcu` is a component for a module whose device is a Tuya MCU behind a UART (a CO2 sensor, an energy
meter, a thermostat ...). The protocol, its handshake and the network state come from `lib/tuya_mcu` of
zephyr-iot-drivers; the YAML says which datapoint is which channel. A channel is in the base unit of its
quantity: `per_count` is that unit per count of the datapoint (a number or a fraction, `0.1`, `1/3`), and the
clusters take the channels as they take any sensor's (`source: air.co2`), with their own multiplier and divisor.
Nothing is reported until the MCU has sent the datapoint. See `examples/tuya-co2-r02.yaml`.

    components:
      - id: air
        type: tuya_mcu
        uart: {tx: mcu_tx, rx: mcu_rx, baud: 9600}      # names from pins:
        channels:
          co2:         {dp: 2,  type: value, per_count: 1}
          temperature: {dp: 12, type: value, per_count: 0.1}

Types: `value` (signed 32 bit), `bool`, `enum`. Read only so far (MCU to Zigbee), TLSR8258 only (UART0, TX one
of PA2 PB1 PC2 PD0 PD7, RX one of PA0 PB0 PB7 PC3 PC5 PD6).

## Chips
| Chip | Toolchain | Status |
|---|---|---|
| `tlsr8258` | TC32 LLVM (`LLVM_TOOLCHAIN_PATH`) | everything; MCUboot loader, OTA, sleepy end device |
| `nrf52840` | GNU Arm Embedded (`GNUARMEMB_TOOLCHAIN_PATH`, default: where `arm-none-eabi-gcc` is) | everything except sleep: sensors (I2C, SAADC on VDD), IAS, plugs and relays, switch, light (4 PWM channels), custom cluster; MCUboot, signed image, OTA file; no sleepy |
| `esp32h2` | Zephyr SDK (`ZEPHYR_SDK_INSTALL_DIR`, default: the newest `~/zephyr-sdk-*`) | router and awake end device: relays, plugs, custom clusters; MCUboot, signed image, OTA file; no sleepy, no ADC battery, no PWM yet |
| `esp32c6` | Zephyr SDK | as the ESP32-H2: router and awake end device; MCUboot, signed image, OTA file; no sleepy, no ADC battery, no PWM yet. Board: ESP32-C6-DevKitC (HP core) |
| `bl702` | Zephyr SDK | router and awake end device: relays, plugs, sensors on I2C (hardware on GPIO0/1, 1/2, 8/9, 14/15, else bit-banged); **MCUboot**, signed image, OTA file (slot 0 at 0x20000, slot 1 at 0x88000, 0x68000 each); no full image: the boot ROM's header is written by the flashing tool (`bflb_mcu_tool` for `*-loader.bin`, then the signed image at 0x20000); needs the BL702 board of MCUboot (`modern-tc32/mcuboot`, branch `tc32-port`, pinned in `docker/pins.env`); **built, not run on hardware**; no sleepy yet; no ADC battery, no PWM yet. Board: XT-ZB1 (BL702C10) |
| `phy6252` | GNU Arm Embedded, or the ARM toolchain of the Zephyr SDK | the Ai-Thinker **PB-03F**: router and awake end device, bit-banged I2C; **MCUboot with one slot**: the loader image (`zephyr.phy6252.bin`, ROM table at 0x2000, code XIP at 0x11002400, 32 KB) and the application signed for slot 0 at 0x10000; the full image is flashed at 0x2000 (`rdwr_phy62x2.py`), later the application is replaced through the loader's serial recovery (mcumgr on UART0, P9/P10). **No OTA file**: the XIP window of the chip is 127 KB and the image fills it, so there is no room for slot 1. Needs the PB-03F board of MCUboot and the SoC image script of the Zephyr fork (see below). The fork's driver is experimental, **built, not run on hardware**; the flash is nearly full (a plug router: 99 %); no sleepy, no ADC battery, no PWM |

nRF52840: pins are `P0.13` (port 0, pin 13; `P0.09`, `P0.10` NFC and `P0.18` reset are reserved), the battery is the
supply itself (SAADC on VDD), a light has four PWM channels. Flash: MCUboot at 0, slot 0 at 0xC000, slot 1 at 0x82000.
The boards of the examples are in `examples/boards/nrf52840` (an nRF52840-DK).

Pin names: ESP32-C6 and BL702 `GPIO10` (reserved: C6 GPIO12/13 USB and 24..29 flash; BL702 GPIO7/8 USB and 23..28 flash);
PB-03F the pad as on the chip, `P2`, `P11` (the console P9/P10, the crystal P16/P17 and P1, P25..P27 are reserved).
The boards of the examples are in `examples/boards/{esp32c6,bl702,phy6252}`.

Sleep, on the nRF52840, ESP32-H2 and ESP32-C6, and on the BL702 and PHY6252 for now: the stack has no light sleep, and its deep sleep needs a retained memory the board does not
describe, so `sleepy: true` is an error there. OTA works through `west build --sysbuild` (MCUboot and the application
signed for slot 0, with the development key of the Zephyr MCUboot); it needs the `tf-psa-crypto` and
`tf-psa-crypto-drivers` modules (`west update tf-psa-crypto tf-psa-crypto-drivers` in the Zephyr workspace). The full
image is MCUboot at 0x0, padded to the start of slot 0 (0x20000 on the H2, 0xC000 on the nRF), then the signed image; the OTA file is the signed image for the coordinator.

## Layout
- `zigbee_builder/` CLI, schema (`config.py`), resolver and renderer (`generator.py`), west build + packaging (`build.py`)
- `zigbee_builder/data/clusters/*.yaml` ZCL clusters as data (attributes, scale, reporting defaults)
- `zigbee_builder/data/components/` sensor types; kinds (`sensor_i2c`, `battery_adc`) in `generator.py`
- `zigbee_builder/data/chips/<chip>/` everything chip specific (board, flash layout, pin names, pinctrl, sleep)
- `zigbee_builder/data/runtime/` C runtime copied into every generated project (from zigbee-devices-firmware)
- `zigbee_builder/data/templates/` Jinja2 templates of the generated files

## Status
M0 (see plan): TLSR8258, sleepy/awake end device and router, temperature/humidity/battery.
Built and compared with `tuya-ts0201-ih-k009` (same Kconfig; 135896 vs 135336 bytes). Not flashed yet.
IAS Zone (door, leak and other zone types) from `binary_input` components: enrollment and
zone status in NV, wake by the pin, notification on change; builds for TLSR8258 (`examples/door-sensor.yaml`,
`leak-sensor.yaml`), not flashed.
Relay (`on_off` with start-up mode off/on/previous kept in NV, button toggle, status LED) and
energy meter (BL0937; Electrical Measurement + Metering, energy total saved to NV every 100 Wh):
`examples/plug-relay.yaml`, `plug-bl0937.yaml`. The BL0937 plug has the same Kconfig as the
`zbeacon-ts011f-bl0937` firmware (154892 vs 154472 bytes), not flashed.
On/Off client switch (`on_off_switch`: toggle/on/off, optional release action; a pin wakes the device,
the command goes to the binding) and several endpoints: `examples/battery-switch.yaml` (two buttons, two
endpoints), `relay-3ch.yaml` (3 relays + their buttons + a wall switch input; the cluster table of the stack
grows to fit). Both build, not flashed.
Custom clusters (`custom_clusters:` with typed attributes, persistence, reporting, commands, manufacturer
code) with C plugins (`plugins:`; the generated `zb_user.h` is their API: `zb_<cluster>_get/set_<attr>`,
`on_write`, `on_command`, `_send`, `zb_user_init`, `zb_user_measure`): `examples/custom-cluster.yaml`,
`plugins/heater.c`. Abstract `binary_input` and `analog_input` clusters: `examples/abstract-sensor.yaml`.
The TC32 toolchain has no soft-float: Analog Input values are built with integer arithmetic
(`zbr_float_bits`), and custom attributes have no float type. All build, not flashed.
PWM lights (`pwm_light`: red/green/blue/warm/cool in any sensible set; On/Off, Level and Color Control:
hue/saturation, xy, colour temperature, step commands, transitions; state in NV): `examples/light-rgbww.yaml`.
PWM channels are assigned from the chip's pin table and conflicts are reported. The colour maths is integer
only and checked on the host against known colours; Level Move runs to its end and Stop is ignored, hue/colour
Move and colour loop are unsupported. This is the first use of Color Control in the stack: builds, not flashed.
Second chip: the ESP32-H2 images (about 270 KB of flash) build, are signed and packed as OTA files; not flashed. Not done: calibration settings in NV,
ESP32-H2 sleep/ADC/PWM, other chips, HA add-on. `app_common.c` still has TLSR-specific wake code (guarded by Kconfig).

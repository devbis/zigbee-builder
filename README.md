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
A pin may carry one name per chip, so one file builds for both: `relay: {tlsr8258: PD2, esp32h2: GPIO12}`,
and `--chip esp32h2` picks the chip without editing the file (`examples/plug-relay.yaml`, `plug-bl0937.yaml`,
`relay-3ch.yaml`, `custom-cluster.yaml` are written that way).

## Chips
| Chip | Toolchain | Status |
|---|---|---|
| `tlsr8258` | TC32 LLVM (`LLVM_TOOLCHAIN_PATH`) | everything; MCUboot loader, OTA, sleepy end device |
| `esp32h2` | Zephyr SDK (`ZEPHYR_SDK_INSTALL_DIR`, default: the newest `~/zephyr-sdk-*`) | router and awake end device: relays, plugs, custom clusters build; no sleepy, no OTA, no ADC battery, no PWM yet |

ESP32-H2 limits, and why: the stack has no light sleep, and its deep sleep needs a retained memory the board does not
describe, so `sleepy: true` is an error there. OTA needs MCUboot through sysbuild, and the workspace lacks the
`tf-psa-crypto` and `tf-psa-crypto-drivers` modules it needs (`west update tf-psa-crypto tf-psa-crypto-drivers`
in the Zephyr workspace; not done, it changes the workspace). The image is flashed at offset 0.

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
Second chip: the ESP32-H2 images build (about 268 KB of flash) and are not flashed. Not done: calibration settings in NV,
ESP32-H2 sleep/OTA/ADC/PWM, other chips, Docker image, HA add-on. `app_common.c` still has TLSR-specific wake code (guarded by Kconfig).

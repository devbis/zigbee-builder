"""Resolve a Config against the catalogs and render a Zephyr project."""
from __future__ import annotations

import json
import math
import re
import shutil
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import jinja2
import yaml

from .chip import Chip, ConfigError, DATA, Pin
from .config import ComponentCfg, Config
from .units import millis, seconds

ACCESS = {"R": "ACCESS_CONTROL_READ", "RW": "(ACCESS_CONTROL_READ | ACCESS_CONTROL_WRITE)",
          "RR": "(ACCESS_CONTROL_READ | ACCESS_CONTROL_REPORTABLE)",
          "RWR": "(ACCESS_CONTROL_READ | ACCESS_CONTROL_WRITE | ACCESS_CONTROL_REPORTABLE)"}
# user type -> (ZCL data type, C type, stdint type of the plugin API)
CUSTOM_TYPES = {
    "bool": ("BOOLEAN", "u8", "bool"), "uint8": ("UINT8", "u8", "uint8_t"),
    "uint16": ("UINT16", "u16", "uint16_t"), "uint32": ("UINT32", "u32", "uint32_t"),
    "int8": ("INT8", "s8", "int8_t"), "int16": ("INT16", "s16", "int16_t"),
    "int32": ("INT32", "s32", "int32_t"), "enum8": ("ENUM8", "u8", "uint8_t"),
    "bitmap8": ("BITMAP8", "u8", "uint8_t"), "bitmap16": ("BITMAP16", "u16", "uint16_t"),
}
ZCL_CLUSTER_NUM_MAX_DEFAULT = 8
ZCL_ATTRIBUTE_NUM_MAX_DEFAULT = 16
COMMON_CLUSTERS = 2   # Basic, Identify on the first endpoint
GROUP_MAX = 4        # reported attributes of one cluster that go out in one command (the default)
GROUP_LIMIT = 8      # what one report frame holds


def load_catalog(kind: str) -> dict:
    out = {}
    for f in sorted((DATA / kind).glob("*.yaml")):
        out[f.stem] = yaml.safe_load(f.read_text())
    return out


def ident(s: str) -> str:
    return re.sub(r"\W", "_", s)


@dataclass
class Binding:
    ep: int
    cluster: dict
    name: str
    attr: dict
    var: str
    comp: str
    chan: str
    div: int
    map: list | None = None     # [(datapoint value, attribute value)] of a channel that counts whole values


@dataclass
class Ctx:
    cfg: Config
    chip: Chip
    clusters: dict
    pins_used: list[Pin] = field(default_factory=list)
    kconfig: list[str] = field(default_factory=list)
    overlay: list[str] = field(default_factory=list)
    comps: dict = field(default_factory=dict)
    inputs: list[str] = field(default_factory=list)
    gpio: object = None
    ias: list[dict] = field(default_factory=list)
    onoffs: list[dict] = field(default_factory=list)
    leds: list[str] = field(default_factory=list)
    n_store: int = 0
    customs: list[dict] = field(default_factory=list)
    lights: list[dict] = field(default_factory=list)
    needs_serial: bool = False
    pin_wake: bool = True       # a pin can end the chip's deep sleep (chip.yaml sleep: pin_wake)


def _kind_sensor_i2c(ctx: Ctx, c, spec, bus_label):
    if bus_label is None:
        raise ConfigError(f"component '{c.id}': needs an i2c: bus")
    addr = c.model_extra.get("address", spec["address"])
    label = f"c_{ident(c.id)}"
    node = (f"\t{label}: {ident(c.id)}@{addr:x} {{\n\t\tcompatible = \"{spec['compatible']}\";\n"
            f"\t\treg = <0x{addr:x}>;\n\t}};\n")
    ctx.comps[c.id] = {
        "id": ident(c.id), "kind": "sensor_i2c", "label": label, "dt_node": node, "bus": bus_label,
        "chans": {"temperature": "SENSOR_CHAN_AMBIENT_TEMP", "humidity": "SENSOR_CHAN_HUMIDITY"},
        "includes": ["<zephyr/drivers/sensor.h>"],
    }
    ctx.kconfig += ["CONFIG_SENSOR=y", "CONFIG_I2C=y"]


def _kind_battery_adc(ctx: Ctx, c, spec, bus_label):
    if "battery_adc" not in ctx.chip.d:
        raise ConfigError(f"chip {ctx.chip.name} has no battery ADC description")
    empty = int(c.model_extra.get("empty_mv", 2200))
    full = int(c.model_extra.get("full_mv", 3000))
    ctx.comps[c.id] = {
        "id": ident(c.id), "kind": "battery_adc", "empty_mv": empty, "full_mv": full,
        "raw_to_mv": bool(ctx.chip.d["battery_adc"].get("raw_to_mv")),
        "chans": {"voltage": None, "percent": None}, "includes": ["<zephyr/drivers/adc.h>"],
    }
    ctx.kconfig += ["CONFIG_ADC=y"]
    ctx.overlay.append((ctx.chip.dir / ctx.chip.d["battery_adc"]["template"]).read_text())


def _ms(cid: str, key: str, value, lo: int, hi: int) -> int:
    """A duration option of a component, in milliseconds, between lo and hi."""
    try:
        ms = millis(value)
    except ValueError:
        raise ConfigError(f"component '{cid}': {key}: {value!r} is not a duration (500ms, 4s)")
    if not lo <= ms <= hi:
        raise ConfigError(f"component '{cid}': {key}: {value!r} is out of {lo} ms .. {hi} ms")
    return ms


def _kind_binary_input(ctx: Ctx, c, spec, bus_label):
    ref = c.model_extra.get("pin")
    if ref is None:
        raise ConfigError(f"component '{c.id}': pin: is required")
    p, pc = ctx.gpio(ref)
    label = f"in_{ident(c.id)}"
    flags = ["GPIO_ACTIVE_LOW" if pc.inverted else "GPIO_ACTIVE_HIGH"]
    if pc.pull == "up":
        flags.append("GPIO_PULL_UP")
    elif pc.pull == "down":
        flags.append("GPIO_PULL_DOWN")
    ctx.inputs.append(
        f"\t\t{label}: {label} {{\n\t\t\tgpios = <&{p.node} {p.num} ({' | '.join(flags)})>;\n"
        f"\t\t\tlabel = \"{c.id}\";\n\t\t}};\n")
    deb = millis(c.model_extra.get("debounce", "50ms"))
    release = _ms(c.id, "release_delay", c.model_extra.get("release_delay", 0), 0, 65535)
    ctx.comps[c.id] = {
        "id": ident(c.id), "kind": "binary_input", "label": label, "debounce": deb, "release": release,
        "wake": bool(c.model_extra.get("wake", ctx.pin_wake)), "chans": {"active": None}, "includes": [],
        "users": [],   # (callback statements), filled by the clusters that use it
        "initial": 2, "service": False,
    }


def _toggle_input(ctx: Ctx, cid: str, opts: dict):
    """toggle_input: an input; toggle_on_button: the service button's pin (button:)."""
    if opts.get("toggle_on_button"):
        if not ctx.cfg.button:
            raise ConfigError(f"component '{cid}': toggle_on_button needs button: (the pin of the button)")
        return "svc_button"
    return opts.get("toggle_input")


def _kind_relay(ctx: Ctx, c, spec, bus_label):
    ref = c.model_extra.get("pin")
    if ref is None:
        raise ConfigError(f"component '{c.id}': pin: is required")
    p, pc = ctx.gpio(ref)
    label = f"relay_{ident(c.id)}"
    pol = "GPIO_ACTIVE_LOW" if pc.inverted else "GPIO_ACTIVE_HIGH"
    ctx.leds.append(f"\t\t{label}: {label} {{\n\t\t\tgpios = <&{p.node} {p.num} {pol}>;\n"
                    f"\t\t\tlabel = \"{c.id}\";\n\t\t}};\n")
    led = c.model_extra.get("status_led", "none")
    led = "none" if led is False else led
    if led not in ("none", "same", "opposite"):
        raise ConfigError(f"component '{c.id}': status_led must be none, same or opposite")
    pulse = _ms(c.id, "pulse", c.model_extra.get("pulse", 0), 0, 65535)
    if "pulse" in c.model_extra and pulse < 10:
        raise ConfigError(f"component '{c.id}': pulse: {c.model_extra['pulse']!r} is shorter than 10 ms")
    ctx.comps[c.id] = {
        "id": ident(c.id), "kind": "relay", "label": label, "led": led, "chans": {}, "pulse": pulse,
        "toggle_input": _toggle_input(ctx, c.id, c.model_extra), "includes": [], "users": [],
    }


def _ratio(cid, key, value) -> Fraction:
    """A scale as an exact fraction: from 110/861 or from a number (to 6 digits)."""
    try:
        r = Fraction(str(value)) if isinstance(value, str) else Fraction(float(value))
        r = r.limit_denominator(1_000_000)
    except (ValueError, ZeroDivisionError):
        raise ConfigError(f"component '{cid}': {key}: '{value}' is not a number or a fraction like 110/861")
    if r <= 0:
        raise ConfigError(f"component '{cid}': {key} must be positive")
    return r


def _kind_energy_meter(ctx: Ctx, c, spec, bus_label):
    props, lines = {}, []
    for pname in spec["pins"]:
        ref = c.model_extra.get(pname)
        if ref is None:
            raise ConfigError(f"component '{c.id}': {pname}: (a pin) is required for {c.type}")
        p, _ = ctx.gpio(ref)
        lines.append(f"\t\t{pname}-gpios = <&{p.node} {p.num} GPIO_ACTIVE_HIGH>;\n")
    for k, v in spec["props"].items():
        lines.append(f"\t\t{v['dt']} = <{int(c.model_extra.get(k, v['default']))}>;\n")
    for k, v in spec.get("scales", {}).items():
        if k in c.model_extra:
            r = _ratio(c.id, k, c.model_extra[k])
            lines.append(f"\t\t{v['dt']} = <{r.numerator} {r.denominator}>;\n")
    for k, v in spec["flags"].items():
        if c.model_extra.get(k, v["default"]):
            lines.append(f"\t\t{v['dt']};\n")
    label = f"c_{ident(c.id)}"
    ctx.overlay.append(f"/ {{\n\t{label}: {ident(c.id)} {{\n\t\tcompatible = \"{spec['compatible']}\";\n"
                       + "".join(lines) + "\t};\n};\n")
    ctx.n_store += 1
    ctx.comps[c.id] = {
        "id": ident(c.id), "kind": "energy_meter", "label": label,
        "chans": {"voltage": None, "current": None, "power": None, "energy": None},
        "includes": ["<zephyr/drivers/sensor.h>", "<iot_drivers/energy_meter.h>"],
        "store_item": 0xA0 + ctx.n_store - 1,
        "persist_step_wh": int(c.model_extra.get("persist_step_wh", 100)),
    }
    ctx.kconfig += ["CONFIG_SENSOR=y"]


def _kind_tuya_mcu(ctx: Ctx, c, spec, bus_label):
    """A host MCU on a UART that speaks the Tuya serial protocol: datapoints become channels."""
    x = c.model_extra
    uart = x.get("uart")
    hw = ctx.chip.d.get("uart")
    if hw is None:
        raise ConfigError(f"component '{c.id}': chip {ctx.chip.name} has no UART description for tuya_mcu")
    if not isinstance(uart, dict) or "tx" not in uart or "rx" not in uart:
        raise ConfigError(f"component '{c.id}': uart: {{tx: <pin>, rx: <pin>, baud: 9600}} is required")
    tx, rx = ctx.gpio(uart["tx"])[0], ctx.gpio(uart["rx"])[0]
    for role, p in (("tx", tx), ("rx", rx)):
        if p.name not in hw[role]:
            raise ConfigError(f"component '{c.id}': {p.name} cannot be the UART {role.upper()} of {ctx.chip.name}; "
                              f"{role.upper()} pins: {', '.join(hw[role])}")
    baud = int(uart.get("baud", 9600))
    ctx.overlay.append(jinja2.Template((ctx.chip.dir / hw["template"]).read_text()).render(
        tx=tx.name, rx=rx.name, baud=baud))
    dp_list, seen = [], {}
    for name, ch in (x.get("channels") or {}).items():
        if not isinstance(ch, dict) or "dp" not in ch:
            raise ConfigError(f"component '{c.id}': channel '{name}' needs dp:")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", str(name)):
            raise ConfigError(f"component '{c.id}': channel name '{name}' is lowercase letters, digits and _")
        dp = int(ch["dp"])
        if not 0 <= dp <= 255:
            raise ConfigError(f"component '{c.id}': channel '{name}': dp {dp} is not 0..255")
        if dp in seen:
            raise ConfigError(f"component '{c.id}': dp {dp} is on '{seen[dp]}' and '{name}'")
        seen[dp] = name
        typ = ch.get("type", "value")
        if typ not in DP_TYPES:
            raise ConfigError(f"component '{c.id}': channel '{name}': type '{typ}' is none of "
                              f"{', '.join(DP_TYPES)}")
        r = _ratio(c.id, f"{name}.per_count", ch.get("per_count", 1))
        g = math.gcd(1000 * r.numerator, r.denominator)       # the channel is in milli-units
        num, den = 1000 * r.numerator // g, r.denominator // g
        if num * den >= 2 ** 31:                              # the 32-bit conversion of the generated code
            raise ConfigError(f"component '{c.id}': channel '{name}': per_count {ch.get('per_count')} "
                              f"does not fit the 32-bit conversion")
        write = bool(ch.get("write", False))
        lo = hi = None
        if "range" in ch:
            rng = ch["range"]
            if not write:
                raise ConfigError(f"component '{c.id}': channel '{name}': range: limits what is written, "
                                  f"it needs write: true")
            if not isinstance(rng, list) or len(rng) != 2:
                raise ConfigError(f"component '{c.id}': channel '{name}': range: [min, max] is expected")
            a, b = (Fraction(str(v)) / r for v in rng)
            lo, hi = math.ceil(a), math.floor(b)
            if lo > hi:
                raise ConfigError(f"component '{c.id}': channel '{name}': range {rng} holds no value of the datapoint")
        dp_list.append({"name": name, "dp": dp, "type": DP_TYPES[typ], "num": num, "den": den, "write": write,
                        "lo": lo, "hi": hi})
    if not dp_list:
        raise ConfigError(f"component '{c.id}': channels: is empty")
    ctx.comps[c.id] = {
        "id": ident(c.id), "kind": "tuya_mcu", "uart_node": hw["node"], "dps": dp_list,
        "chans": {d["name"]: None for d in dp_list}, "chan_valid": True,
        "includes": ["<iot_drivers/tuya_mcu.h>", "<iot_drivers/tuya_uart.h>"],
    }
    ctx.kconfig += ["CONFIG_SERIAL=y", "CONFIG_IOTDRV_TUYA_MCU=y", "CONFIG_IOTDRV_TUYA_UART=y"]
    ctx.needs_serial = True


LIGHT_CHANNELS = ("red", "green", "blue", "warm", "cool")


def _kind_pwm_light(ctx: Ctx, c, spec, bus_label):
    if ctx.lights:
        raise ConfigError("one pwm_light per device")
    me = c.model_extra
    me = {("warm" if k == "white" else k): v for k, v in me.items()}
    pins = {ch: me[ch] for ch in LIGHT_CHANNELS if ch in me}
    if not pins:
        raise ConfigError(f"component '{c.id}': give at least one of red, green, blue, warm (or white), cool")
    if ("red" in pins or "green" in pins or "blue" in pins) and not all(k in pins for k in ("red", "green", "blue")):
        raise ConfigError(f"component '{c.id}': a colour lamp needs red, green and blue")
    if "cool" in pins and "warm" not in pins:
        raise ConfigError(f"component '{c.id}': cool needs warm too (a white lamp has just white or warm)")
    resolved = {ch: ctx.gpio(ref) for ch, ref in pins.items()}
    assign = ctx.chip.pwm_assign([p.name for p, _ in resolved.values()])
    chans = {}
    for ch, (p, pc) in resolved.items():
        chans[ch] = {"channel": assign[p.name.upper()], "inverted": pc.inverted or bool(me.get("inverted", False))}
    mir = me.get("mireds", [153, 370])
    if len(mir) != 2 or not 100 <= mir[0] < mir[1] <= 1000:
        raise ConfigError(f"component '{c.id}': mireds: [cool, warm], like [153, 370]")
    pwm = ctx.chip.d["pwm"]
    ctx.overlay.append(jinja2.Template((ctx.chip.dir / pwm["template"]).read_text()).render(
        pins=[{"pin": resolved[ch][0].name.upper(), "channel": chans[ch]["channel"],
               "port": resolved[ch][0].port, "num": resolved[ch][0].num} for ch in chans]))
    has_rgb, has_w = "red" in pins, "warm" in pins
    caps = 0
    if has_rgb:
        caps |= 0x0001 | 0x0008 | 0x0010
    elif "cool" in pins:
        caps |= 0x0010
    ctx.n_store += 1
    comp = {
        "id": ident(c.id), "kind": "pwm_light", "chans": {}, "includes": [], "users": [],
        "pwm_node": pwm["device"], "period": pwm["period_cycles"], "channels": chans,
        "transition_ms": millis(me.get("transition", "300ms")), "mired_min": mir[0], "mired_max": mir[1],
        "store_item": 0xA0 + ctx.n_store - 1, "caps": caps, "has_rgb": has_rgb, "has_ct": "cool" in pins,
        "color_mode": 2 if (has_w and "cool" in pins) else 0, "mireds": min(max(370, mir[0]), mir[1]),
        "ep": None, "toggle_input": _toggle_input(ctx, c.id, me),
    }
    ctx.kconfig += ["CONFIG_PWM=y"]
    ctx.comps[c.id] = comp
    ctx.lights.append(comp)


KINDS = {"pwm_light": _kind_pwm_light, "sensor_i2c": _kind_sensor_i2c, "battery_adc": _kind_battery_adc,
         "binary_input": _kind_binary_input, "relay": _kind_relay,
         "energy_meter": _kind_energy_meter, "tuya_mcu": _kind_tuya_mcu}
DP_TYPES = {"value": "IOTDRV_TUYA_DP_VALUE", "bool": "IOTDRV_TUYA_DP_BOOL", "enum": "IOTDRV_TUYA_DP_ENUM"}


def _type_specs() -> dict:
    specs = {}
    for f in sorted((DATA / "components").glob("*.yaml")):
        specs.update(yaml.safe_load(f.read_text()))
    specs["battery_adc"] = {"kind": "battery_adc"}
    specs["binary_input"] = {"kind": "binary_input"}
    specs["relay"] = {"kind": "relay"}
    specs["pwm_light"] = {"kind": "pwm_light"}
    specs["tuya_mcu"] = {"kind": "tuya_mcu"}
    return specs


def resolve(cfg: Config):
    z = cfg.zigbee
    chip = Chip(z.chip)
    clusters = load_catalog("clusters")
    devices = yaml.safe_load((DATA / "devices.yaml").read_text())
    ctx = Ctx(cfg, chip, clusters)
    ctx.pin_wake = bool((chip.d.get("sleep") or {}).get("pin_wake", True))

    if z.role not in chip.d["roles"]:
        raise ConfigError(f"chip {chip.name} does not support role {z.role}")
    can_sleep = chip.d.get("sleep") is not None
    sleepy = (z.role == "end_device" and can_sleep) if z.sleepy is None else z.sleepy
    if sleepy and z.role != "end_device":
        raise ConfigError("sleepy: true needs role: end_device")
    if sleepy and not can_sleep:
        raise ConfigError(f"sleepy: true is not supported on {chip.name}: the chip pack describes no deep sleep "
                          f"for it (no sleep: in its chip.yaml). Use sleepy: false (the end device stays awake) "
                          f"or a router")
    power = z.power or ("battery" if z.role == "end_device" else "mains")

    if z.role == "end_device" and not 1 <= z.poll_interval <= 60:
        raise ConfigError(f"poll_interval {z.poll_interval}s: the stack polls its parent every 1..60 s "
                          f"(CONFIG_ZIGBEE_ED_POLL_INTERVAL_MS); reports go out by their own min/max intervals")

    # --- pins
    named = {}
    for ref, v in cfg.pins.items():
        pc = cfg.pin_cfg(ref)
        named[ref] = (chip.pin(pc.pin), pc)
    seen = {}
    for ref, (p, _) in named.items():
        if p.name in seen:
            raise ConfigError(f"pin {p.name} used twice: '{seen[p.name]}' and '{ref}'")
        seen[p.name] = ref
        ctx.pins_used.append(p)

    def gpio(ref: str):
        if ref not in named:
            raise ConfigError(f"unknown pin '{ref}'; declare it under pins:")
        return named[ref]

    ctx.gpio = gpio

    # --- i2c buses
    buses = cfg.i2c if isinstance(cfg.i2c, list) else ([cfg.i2c] if cfg.i2c else [])
    bus_labels = {}
    bus_nodes = {}
    for b in buses:
        sda, scl = gpio(b.sda)[0], gpio(b.scl)[0]
        hw = chip.i2c_hw_for(sda.name, scl.name) if b.hw in ("auto", True) else None
        if b.hw is True and hw is None:
            raise ConfigError(f"i2c '{b.id}': {sda.name}/{scl.name} is not a hardware I2C pair on {chip.name}")
        if hw:
            tpl = (chip.dir / hw["template"]).read_text()
            ctx.overlay.append(jinja2.Template(tpl).render(sda=sda.name, scl=scl.name, sda_pin=sda, scl_pin=scl))
            bus_labels[b.id] = "&i2c0"
        else:
            bus_nodes[b.id] = (f"i2c_{ident(b.id)}", sda, scl)
            bus_labels[b.id] = "soft"

    # --- components
    specs = _type_specs()
    comp_nodes: dict[str, list[str]] = {}
    for c in cfg.components:
        spec = specs.get(c.type)
        if spec is None:
            raise ConfigError(f"component '{c.id}': unknown type '{c.type}', known: {', '.join(sorted(specs))}")
        bus_id = c.model_extra.get("i2c", "default")
        if spec["kind"] == "sensor_i2c" and bus_id not in bus_labels:
            raise ConfigError(f"component '{c.id}': i2c bus '{bus_id}' not defined")
        KINDS[spec["kind"]](ctx, c, spec, bus_labels.get(bus_id) if spec["kind"] == "sensor_i2c" else None)
        if spec["kind"] == "sensor_i2c":
            ctx.comps[c.id]["bus_id"] = bus_id
            comp_nodes.setdefault(bus_id, []).append(ctx.comps[c.id]["dt_node"])

    # --- service inputs: the buttons whose long press joins and resets
    if cfg.button:
        _kind_binary_input(ctx, ComponentCfg(id="svc_button", type="binary_input", pin=cfg.button), None, None)
    svc = cfg.service
    service_ids = []
    if svc and svc.inputs == "any":
        service_ids = [i for i, c in ctx.comps.items() if c["kind"] == "binary_input"]
        if not service_ids:
            raise ConfigError("service: inputs: any, but the device has no binary_input component")
    elif svc:
        for i in svc.inputs:
            comp = ctx.comps.get(i)
            if comp is None or comp["kind"] != "binary_input":
                raise ConfigError(f"service: inputs: '{i}' is not a binary_input component")
            service_ids.append(i)
    if cfg.button:
        service_ids = list(dict.fromkeys(["svc_button", *service_ids]))
    for i in service_ids:
        comp = ctx.comps[i]
        if sleepy and ctx.pin_wake and not comp["wake"]:
            raise ConfigError(f"service input '{i}': a sleepy device needs wake: true on it")
        comp["service"] = True
    service = {"hold_ms": (svc.hold if svc else 5) * 1000, "search_ms": (svc.search_time if svc else 60) * 1000,
               "inputs": service_ids, "on_boot": not service_ids,
               "on_power_on": bool(service_ids) and (svc.search_on_power_on if svc else True)}
    if not service_ids:
        service["search_ms"] = 0              # no button: the search stays open until the device joins

    # i2c bus nodes: hw bus gets children under &i2c0, soft bus is a gpio-i2c node
    overlay_tail = []
    for b in buses:
        kids = "".join(comp_nodes.get(b.id, []))
        if b.id not in bus_nodes:
            overlay_tail.append(f"&i2c0 {{\n{kids}}};\n")
        else:
            label, sda, scl = bus_nodes[b.id]
            overlay_tail.append(
                f"/ {{\n\t{label}: i2c {{\n\t\tcompatible = \"gpio-i2c\";\n"
                f"\t\tclock-frequency = <I2C_BITRATE_STANDARD>;\n"
                f"\t\tsda-gpios = <&{sda.node} {sda.num} GPIO_PULL_UP>;\n"
                f"\t\tscl-gpios = <&{scl.node} {scl.num} GPIO_PULL_UP>;\n"
                f"\t\t#address-cells = <1>;\n\t\t#size-cells = <0>;\n\n{kids}\t}};\n}};\n")

    if ctx.leds:
        overlay_tail.append("/ {\n\tleds {\n\t\tcompatible = \"gpio-leds\";\n" + "".join(ctx.leds) + "\t};\n};\n")
    if ctx.inputs:
        overlay_tail.append("/ {\n\tzbr_inputs {\n\t\tcompatible = \"gpio-keys\";\n" + "".join(ctx.inputs) + "\t};\n};\n")

    # led
    if cfg.status_led:
        p, pc = gpio(cfg.status_led)
        pol = "GPIO_ACTIVE_LOW" if pc.inverted else "GPIO_ACTIVE_HIGH"
        if chip.d.get("led0_node", True):
            overlay_tail.append(f"&led0 {{\n\tgpios = <&{p.node} {p.num} {pol}>;\n}};\n")
        else:   # the board has no led0: make it
            overlay_tail.append(
                f"/ {{\n\taliases {{ led0 = &status_led; }};\n\tleds {{\n\t\tcompatible = \"gpio-leds\";\n"
                f"\t\tstatus_led: status_led {{\n\t\t\tgpios = <&{p.node} {p.num} {pol}>;\n"
                f"\t\t\tlabel = \"Status\";\n\t\t}};\n\t}};\n}};\n")

    if sleepy and not ctx.pin_wake:
        # the pins cannot end this chip's sleep: an input is seen only while it is awake. The service button is
        # fine (the device does not sleep until it has joined), a sensor input would miss what happens asleep.
        for i, c in ctx.comps.items():
            if c["kind"] == "binary_input" and i != "svc_button":
                raise ConfigError(f"component '{i}': {chip.name} has no pin wake from its deep sleep, a sleepy "
                                  f"device would miss what the input does while it sleeps. Use sensors read "
                                  f"on a timer, or sleepy: false")

    # --- endpoints
    eps, bindings, writes = [], [], []
    for ei, e in enumerate(cfg.endpoints):
        if e.device not in devices:
            raise ConfigError(f"endpoint {e.id}: unknown device '{e.device}', known: {', '.join(devices)}")
        ep = {"id": e.id, "dev_id": devices[e.device], "dev_ver": e.version, "clusters": [],
              "first": ei == 0, "out": [], "switch": False}
        for cname, ccfg in e.clusters.items():
            ccfg = dict(ccfg or {})
            cat = clusters.get(cname)
            custom = next((c for c in cfg.custom_clusters if c.name == cname), None)
            if cat is None and custom is not None:
                _custom(ctx, e, ep, ccfg, custom, sleepy)
                continue
            if cat is None:
                raise ConfigError(f"endpoint {e.id}: unknown cluster '{cname}', known: {', '.join(clusters)}")
            ctx.kconfig += [k for k in cat.get("kconfig", [])]
            cl = {"name": cname, "cat": cat, "attrs": [], "var": f"ep{e.id}_{cname}", "ias": None, "onoff": None, "custom": None, "light": None, "light_kind": None}
            if cat.get("special") == "ias_zone":
                _ias(ctx, e, cl, ccfg, cat, sleepy)
                ep["clusters"].append(cl)
                continue
            if cat.get("special") in ("light_level", "light_color"):
                _light_cluster(ctx, e, cl, ccfg, cat)
                ep["clusters"].append(cl)
                continue
            if cat.get("special") == "on_off_switch":
                ctx.kconfig += cat.get("kconfig", [])
                _switch(ctx, e, ep, ccfg, cat, sleepy)
                continue
            if cat.get("special") == "on_off":
                _onoff(ctx, e, cl, ccfg, cat)
                ep["clusters"].append(cl)
                continue
            extras, ccfg = _extras(cname, ccfg, e.id)
            if extras:
                cat = {**cat, "attributes": {**cat["attributes"], **extras}}
            srcs, owners = _bindings_of(cname, cat, ccfg, e.id)
            for aname, a in cat["attributes"].items():
                if aname in owners:                       # multiplier / divisor of a measured attribute
                    owner, value = owners[aname]
                    if srcs[owner][0] is None:            # the value is not bound: no pair either
                        continue
                    a = {**a, "const": value}
                elif "pair" in a:
                    mult, dvs = _pair_of(cat, a, srcs[aname][1], cname, aname)
                    a = {**a, "scale": mult / dvs, "pair_vals": (mult, dvs)}
                elif a.get("extra") or ("scale" in a and ("multiplier" in srcs[aname][1]
                                                          or "divisor" in srcs[aname][1])):
                    # an attribute of the device, or a step of the binding's own: multiplier / divisor
                    mult, dvs = _step_of(cname, aname, srcs[aname][1])
                    a = {**a, "scale": mult / dvs, "pair_vals": (mult, dvs)}
                elif "scale" in ccfg and aname == _first_measured(cat):
                    a = {**a, "scale": float(ccfg["scale"])}
                v = f"{cl['var']}_{aname}"
                at = {"name": aname, "a": a, "var": v, "access": ACCESS[a["access"]]}
                if "const" in a:
                    at["const"] = a["const"]
                    cl["attrs"].append(at)
                    continue
                src = srcs[aname][0]
                if src is None:
                    cl["attrs"].append({**at, "init": a.get("invalid", 0), "unbound": True})
                    continue
                comp, _, chan = str(src).partition(".")
                chan = chan or a["channel"]
                if comp not in ctx.comps:
                    raise ConfigError(f"endpoint {e.id}/{cname}: source '{src}': no component '{comp}'")
                if chan not in ctx.comps[comp]["chans"]:
                    raise ConfigError(f"endpoint {e.id}/{cname}: component '{comp}' has no channel '{chan}'"
                                      f" (has: {', '.join(ctx.comps[comp]['chans'])})")
                if "pair_vals" in a:        # the channel is in milli-units: the step must be a whole number of them
                    mult, dvs = a["pair_vals"]
                    if (1000 * mult) % dvs:
                        raise ConfigError(f"endpoint {e.id}/{cname}.{aname}: multiplier/divisor {mult}/{dvs} is "
                                          f"finer than the channel unit (a multiple of 1/1000 is needed)")
                    div = 1000 * mult // dvs
                else:
                    div = round(a["scale"] * 1000)
                if div < 1:
                    raise ConfigError(f"{cname}.{aname}: scale {a['scale']} is finer than the channel unit")
                at.update(init=a.get("invalid", 0), comp=comp, chan=chan, div=div)
                bmap, wr = _write_and_map(ctx, e.id, cname, aname, a, comp, chan, srcs[aname][1], at)
                if wr:
                    writes.append(_write_of(e.id, cat, a, comp, chan, div, ctx.comps[comp], bmap))
                if a.get("event"):
                    if ctx.comps[comp]["kind"] != "binary_input":
                        raise ConfigError(f"endpoint {e.id}/{cname}: source '{src}' is not a binary_input component")
                    ctx.comps[comp]["initial"] = 0
                    ctx.comps[comp]["users"].append(
                        f"if ({v} != (u8)active) {{ {v} = active; zcl_setAttrVal({e.id}, {cat['id']}, "
                        f"{a['id']}, (u8 *)&{v}); " + ("zb_schedule_report(); " if sleepy else "") + "}")
                # reporting defaults, user override in physical units
                rep = dict(a.get("report", {}))
                rep.update(ccfg.get("report", {}) if aname == _first_measured(cat) else {})
                rep.update(srcs[aname][1].get("report", {}))
                if "report" in a or rep:
                    r = {
                        "min": seconds(rep["min"]), "max": seconds(rep["max"]),
                        "change": round(float(rep.get("change", 0)) / a["scale"]),
                    }
                    at["report"] = r
                cl["attrs"].append(at)
                if not a.get("event"):
                    bindings.append(Binding(e.id, cl, aname, at, v, comp, chan, div, bmap))
            ep["clusters"].append(cl)
        eps.append(ep)

    # the stack's cluster table is one for all endpoints; Basic and Identify count once
    n_total = COMMON_CLUSTERS + sum(len(ep["clusters"]) for ep in eps)
    ctx_cluster_max = max(ZCL_CLUSTER_NUM_MAX_DEFAULT, n_total)
    max_attrs = max((len(c["attrs"]) + 1 for ep in eps for c in ep["clusters"]), default=0)

    # ordered report items: per endpoint, per cluster (adjacent)
    items = []
    for ep in eps:
        for cl in ep["clusters"]:
            for at in cl["attrs"]:
                if "report" in at and sleepy and "invalid" in at["a"] and at["a"]["c_type"] != "u64":
                    items.append((ep["id"], cl, at))
    defaults = [(ep["id"], cl, at) for ep in eps for cl in ep["clusters"] for at in cl["attrs"] if "report" in at]
    # ota file / report group limits
    group_max = GROUP_MAX
    for ep in eps:
        for cl in ep["clusters"]:
            n_rep = sum(1 for a in cl["attrs"] if "report" in a)
            if n_rep > GROUP_LIMIT:
                raise ConfigError(f"cluster {cl['name']}: more than {GROUP_LIMIT} reported attributes")
            group_max = max(group_max, n_rep)

    return {
        "cfg": cfg, "chip": chip, "sleepy": sleepy, "power": power, "eps": eps, "ctx": ctx,
        "bindings": bindings, "writes": writes, "defaults": defaults, "inputs": ctx.inputs, "inputs_used": bool(ctx.inputs), "onoffs": ctx.onoffs, "lights": ctx.lights, "service": service, "customs": ctx.customs, "switches": any(ep["switch"] for ep in eps), "ias": ctx.ias, "items": items, "cluster_max": ctx_cluster_max, "group_max": group_max,
        "attr_max": max(ZCL_ATTRIBUTE_NUM_MAX_DEFAULT, max_attrs), "overlay_tail": overlay_tail,
        "ports": chip.ports(ctx.pins_used),
    }


def _word(v):
    """YAML 1.1 reads a bare on/off as a boolean: give the word back."""
    return {True: "on", False: "off"}.get(v, v) if isinstance(v, bool) else v


def _custom(ctx: Ctx, e, ep, ccfg, cc, sleepy):
    if any(c["name"] == cc.name for c in ctx.customs):
        raise ConfigError(f"custom cluster '{cc.name}' is used on two endpoints; one instance per device")
    if ccfg:
        raise ConfigError(f"endpoint {e.id}/{cc.name}: a custom cluster takes no options here")
    var = f"ep{e.id}_{cc.name}"
    cat = {"id": f"0x{cc.id:04X}", "header": None, "register": f"{var}_register", "kconfig": []}
    cl = {"name": cc.name, "cat": cat, "attrs": [], "var": var, "ias": None, "onoff": None, "custom": None, "light": None, "light_kind": None}
    persisted, size = [], 0
    ids = set()
    for a in cc.attributes:
        if a.id in ids:
            raise ConfigError(f"custom cluster {cc.name}: attribute id 0x{a.id:04X} twice")
        ids.add(a.id)
        zt, ct, api = CUSTOM_TYPES[a.type]
        reportable = a.report is not None
        acc = ("RW" if a.access == "rw" else "R") + ("R" if reportable and a.access == "rw" else "")
        if reportable and a.access == "r":
            acc = "RR"
        ad = {"id": f"0x{a.id:04X}", "type": zt, "c_type": ct}
        if a.invalid is not None:
            ad["invalid"] = a.invalid
        at = {"name": a.name, "a": ad, "var": f"{var}_{a.name}", "access": ACCESS[acc],
              "init": float(a.default) if a.type == "float" else int(a.default), "unbound": True,
              "api": api, "persist": a.persist, "ep": e.id, "cluster_id": cat["id"]}
        if reportable:
            r = a.report
            at["report"] = {"min": seconds(r.get("min", "10s")), "max": seconds(r.get("max", "5min")),
                            "change": int(r.get("change", 0))}
        if a.persist:
            size += {"u8": 1, "s8": 1, "u16": 2, "s16": 2, "u32": 4, "s32": 4, "float": 4}[ct]
            persisted.append(at)
        cl["attrs"].append(at)
    if size > 32:
        raise ConfigError(f"custom cluster {cc.name}: persisted attributes take {size} bytes, at most 32")
    ctx.n_store += 1 if persisted else 0
    cl["custom"] = {"name": cc.name, "ep": e.id, "manu": cc.manufacturer_code, "id": cat["id"],
                    "commands": [(c.id, c.name) for c in cc.commands], "persisted": persisted,
                    "store_item": 0xA0 + ctx.n_store - 1, "attrs": cl["attrs"], "var": var}
    ctx.customs.append(cl["custom"])
    ep["clusters"].append(cl)


def _use_input(ctx, name, mode, who):
    comp = ctx.comps.get(name)
    if comp is None or comp["kind"] != "binary_input":
        raise ConfigError(f"{who}: '{name}' is not a binary_input component")
    comp["initial"] = min(comp["initial"], mode)
    return comp


def _switch(ctx, e, ep, ccfg, cat, sleepy):
    who = f"endpoint {e.id}/on_off_switch"
    extra = set(ccfg) - {"source", "action", "release_action"}
    if extra:
        raise ConfigError(f"{who}: unknown keys {sorted(extra)}")
    act = _word(ccfg.get("action", "toggle"))
    rel = _word(ccfg.get("release_action"))
    for a in (act, rel):
        if a is not None and a not in cat["actions"]:
            raise ConfigError(f"{who}: action must be one of {', '.join(cat['actions'])}, got {a!r}")
    comp = _use_input(ctx, ccfg.get("source"), 1, who)
    if sleepy and not comp["wake"]:
        raise ConfigError(f"{who}: a sleepy switch needs wake: true on '{ccfg['source']}'")
    stmt = f"if (active) {{ zbr_switch_send({e.id}, {cat['actions'][act]}); }}"
    if rel:
        stmt = stmt[:-1] + f"}} else {{ zbr_switch_send({e.id}, {cat['actions'][rel]}); }}"
    comp["users"].append(stmt)
    ep["out"].append(cat["out_cluster"])
    ep["switch"] = True


def _ias(ctx: Ctx, e, cl, ccfg, cat, sleepy):
    zt = ccfg.get("zone_type")
    if zt not in cat["zone_types"]:
        raise ConfigError(f"endpoint {e.id}/ias_zone: zone_type must be one of "
                          f"{', '.join(cat['zone_types'])}, got {zt!r}")
    if "source" in ccfg:
        ccfg.setdefault("alarm1", ccfg.pop("source"))
    extra = set(ccfg) - {"zone_type", *cat["bits"]}
    if extra:
        raise ConfigError(f"endpoint {e.id}/ias_zone: unknown keys {sorted(extra)}")
    if "alarm1" not in ccfg:
        raise ConfigError(f"endpoint {e.id}/ias_zone: alarm1 (or source) is required")
    idx = len(ctx.ias)
    z = {"var": f"ep{e.id}_ias", "ep": e.id, "zone_type": cat["zone_types"][zt], "slot": idx,
         "bits": []}
    for key, mask in cat["bits"].items():
        if key not in ccfg:
            continue
        comp = _use_input(ctx, ccfg[key], 0, f"endpoint {e.id}/ias_zone: {key}")
        comp["users"].append(f"zbr_ias_set(&{z['var']}, 0x{mask:04X}, active);")
        if sleepy and not comp["wake"]:
            raise ConfigError(f"component '{ccfg[key]}': a sleepy IAS sensor needs wake: true on its input")
        z["bits"].append((key, ccfg[key]))
    ctx.ias.append(z)
    cl["ias"] = z


def _light_ep(comp, ep_id):
    if comp["ep"] not in (None, ep_id):
        raise ConfigError(f"the light '{comp['id']}' is on endpoint {comp['ep']} and {ep_id}; "
                          f"its On/Off, Level and Color Control clusters belong to one endpoint")
    comp["ep"] = ep_id


def _light_cluster(ctx, e, cl, ccfg, cat):
    comp = ctx.comps.get(ccfg.get("source"))
    if comp is None or comp["kind"] != "pwm_light":
        raise ConfigError(f"endpoint {e.id}/{cl['name']}: source: '{ccfg.get('source')}' is not a pwm_light component")
    _light_ep(comp, e.id)
    if cat["special"] == "light_color" and not (comp["has_rgb"] or comp["has_ct"]):
        raise ConfigError(f"endpoint {e.id}/color_control: the light '{comp['id']}' has no RGB or warm+cool "
                          f"channels; a white lamp takes level only")
    cl["light"] = comp
    cl["light_kind"] = cat["special"]


def _onoff(ctx: Ctx, e, cl, ccfg, cat):
    comp = ctx.comps.get(ccfg.get("source"))
    if comp is None or comp["kind"] not in ("relay", "pwm_light"):
        raise ConfigError(f"endpoint {e.id}/on_off: source: '{ccfg.get('source')}' is not a relay or pwm_light component")
    if comp["kind"] == "pwm_light":
        _light_ep(comp, e.id)
    pulse = comp.get("pulse", 0)
    startup = _word(ccfg.get("startup", "off" if pulse else "previous"))
    if startup not in ("off", "on", "previous"):
        raise ConfigError(f"endpoint {e.id}/on_off: startup must be off, on or previous")
    if pulse and startup != "off":
        raise ConfigError(f"endpoint {e.id}/on_off: '{comp['id']}' is a pulse: it starts off, "
                          f"startup: {startup} makes no sense")
    ctx.n_store += 1
    o = {"var": f"ep{e.id}_onoff", "ep": e.id, "relay": comp, "startup": ("off", "on", "previous").index(startup),
         "item": 0xA0 + ctx.n_store - 1, "pulse": pulse,
         "report": {k: seconds(v) for k, v in cat["report"].items()}}
    comp["onoff_var"] = o["var"]
    ti = comp.get("toggle_input")
    if ti:
        inp = _use_input(ctx, ti, 2, f"component '{comp['id']}': toggle_input")
        inp["users"].append(f"if (active) {{ zbr_onoff_set(&{o['var']}, !{o['var']}.state); }}")
    ctx.onoffs.append(o)
    cl["onoff"] = o


def _bindings_of(cname, cat, ccfg, ep_id):
    """The source of every attribute of a cluster (with its options) and the multiplier/divisor
    attributes that belong to a measured one. An attribute is bound by "comp.chan" or by
    {source: comp.chan, multiplier: 1, divisor: 100}; the pair is read-only and added to the cluster only
    together with its bound value, so it is never bound on its own."""
    attrs = cat["attributes"]
    members = {}
    for an, a in attrs.items():
        for role, pn in (a.get("pair") or {}).items():
            members[pn] = an
    for pn in members:
        if ccfg.get(pn) is not None:
            raise ConfigError(f"endpoint {ep_id}/{cname}: '{pn}' is derived from {members[pn]}'s "
                              f"multiplier/divisor, it takes no source")
    srcs = {}
    for an, a in attrs.items():
        if "const" in a:
            continue
        bind, opts = ccfg.get(an), {}
        if isinstance(bind, dict):
            opts = dict(bind)
            bind = opts.pop("source", None)
        short = ccfg.get("source")
        if bind is None and short is not None and ("." not in str(short) or an == _first_measured(cat)):
            bind = short
        unknown = set(opts) - {"multiplier", "divisor", "report", "write", "map"}
        if unknown:
            raise ConfigError(f"endpoint {ep_id}/{cname}.{an}: unknown option {', '.join(sorted(unknown))} "
                              f"(source, write, map, multiplier, divisor, report)")
        if ("multiplier" in opts or "divisor" in opts) and "pair" not in a and not a.get("extra") \
                and "scale" not in a:
            raise ConfigError(f"endpoint {ep_id}/{cname}.{an}: it has no multiplier/divisor of its own")
        srcs[an] = (bind, opts)
    owners = {}
    for an, a in attrs.items():
        if "pair" in a:
            mult, dvs = _pair_of(cat, a, srcs[an][1], cname, an)
            owners[a["pair"]["multiplier"]] = (an, mult)
            owners[a["pair"]["divisor"]] = (an, dvs)
    return srcs, owners


def _step_of(cname, aname, opts):
    """multiplier and divisor of an attribute of the device (the default is 1/1)."""
    out = []
    for role in ("multiplier", "divisor"):
        val = opts.get(role, 1)
        if not isinstance(val, int) or isinstance(val, bool) or val < 1:
            raise ConfigError(f"{cname}.{aname}: {role} must be a positive integer, got {val!r}")
        out.append(val)
    return out[0], out[1]


def _extras(cname, ccfg, ep_id):
    """Attributes of the device in a standard cluster (extra: {0xF001: {name, type, source, write ...}}):
    catalog-like entries, and the cluster's config with each one's options under its name."""
    extra = ccfg.get("extra")
    if not extra:
        return {}, ccfg
    if not isinstance(extra, dict):
        raise ConfigError(f"endpoint {ep_id}/{cname}: extra: is {{attribute id: {{name, type, source}}}}")
    attrs, ccfg = {}, dict(ccfg)
    for aid, d in extra.items():
        where = f"endpoint {ep_id}/{cname} extra {aid!r}"
        if not isinstance(aid, int) or not 0 <= aid <= 0xFFFF:
            raise ConfigError(f"{where}: the attribute id is a number 0..0xFFFF")
        if not isinstance(d, dict) or "name" not in d or "type" not in d:
            raise ConfigError(f"{where}: name: and type: are required")
        name, typ = str(d["name"]), d["type"]
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ConfigError(f"{where}: name '{name}' is lowercase letters, digits and _")
        if typ not in CUSTOM_TYPES or typ == "float":
            raise ConfigError(f"{where}: type '{typ}' is none of "
                              f"{', '.join(t for t in CUSTOM_TYPES if t != 'float')}")
        if name in ccfg or any(a["id"] == aid for a in attrs.values()):
            raise ConfigError(f"{where}: '{name}' or its id is used twice")
        if "source" not in d:
            raise ConfigError(f"{where}: source: is required (a channel of a component)")
        zt, ct, _ = CUSTOM_TYPES[typ]
        lo, hi = {"u8": (0, 255), "s8": (-128, 127), "u16": (0, 0xFFFF), "s16": (-32768, 32767),
                  "u32": (0, 0xFFFFFFFF), "s32": (-2 ** 31, 2 ** 31 - 1)}[ct]
        access = "RWR" if d.get("write") else "RR"
        a = {"id": f"0x{aid:04X}", "type": zt, "c_type": ct, "access": access, "channel": "", "scale": 1,
             "min": lo, "max": hi, "extra": True, "report": {"min": "10s", "max": "5min", "change": 0}}
        attrs[name] = a
        ccfg[name] = {k: v for k, v in d.items() if k not in ("name", "type")}
    return attrs, ccfg


def _map_table(cname, aname, raw, a):
    """A map {datapoint value: attribute value} as pairs of numbers; the attribute side may name the
    attribute's enum values (system_mode: heat)."""
    if not isinstance(raw, dict) or not raw:
        raise ConfigError(f"{cname}.{aname}: map: is {{datapoint value: attribute value}}")
    enum = a.get("enum", {})
    out = []
    for k, v in raw.items():
        if isinstance(k, bool):
            k = int(k)
        if not isinstance(k, int):
            raise ConfigError(f"{cname}.{aname}: map key {k!r} is a number or a boolean")
        if isinstance(v, bool):           # YAML 1.1 reads the words off and on as booleans
            name = "on" if v else "off"
            v = enum[name] if name in enum else int(v)
        if isinstance(v, str):
            if v not in enum:
                raise ConfigError(f"{cname}.{aname}: map value '{v}' is none of "
                                  f"{', '.join(enum) or '(this attribute has no names: use numbers)'}")
            v = enum[v]
        if not isinstance(v, int):
            raise ConfigError(f"{cname}.{aname}: map value {v!r} is a number or a name")
        if not a["min"] <= v <= a["max"]:
            raise ConfigError(f"{cname}.{aname}: map value {v} is out of the attribute's range")
        out.append((k, v))
    return out


def _write_and_map(ctx, ep_id, cname, aname, a, comp, chan, opts, at):
    """The binding's write: and map:. Returns (map or None, writes); a writable attribute whose binding does
    not write is read-only."""
    where = f"endpoint {ep_id}/{cname}.{aname}"
    wr = bool(opts.get("write", False))
    c = ctx.comps[comp]
    d = next((x for x in c.get("dps", []) if x["name"] == chan), None)
    if wr:
        if "W" not in a["access"]:
            raise ConfigError(f"{where}: the attribute is read-only, it takes no write:")
        if d is None:
            raise ConfigError(f"{where}: component '{comp}' takes no writes")
        if not d["write"]:
            raise ConfigError(f"{where}: channel '{chan}' of '{comp}' has no write: true")
    else:
        at["access"] = ACCESS[{"RW": "R", "RWR": "RR"}.get(a["access"], a["access"])]
    mp = opts.get("map")
    if mp is None:
        return None, wr
    if d is None or (d["num"], d["den"]) != (1000, 1):
        raise ConfigError(f"{where}: map: needs a datapoint channel that counts whole values (per_count: 1)")
    table = _map_table(cname, aname, mp, a)
    if wr and len({v for _, v in table}) != len(table):
        raise ConfigError(f"{where}: a map that is written must be reversible: a value is on two datapoint values")
    return table, wr


def _write_of(ep_id, cat, a, comp, chan, div, c, bmap):
    """What the generated code does when the coordinator writes this attribute."""
    where = f"endpoint {ep_id}/{cat['name']}.{chan}"
    d = next(x for x in c["dps"] if x["name"] == chan)
    if a["c_type"] == "float":
        raise ConfigError(f"{where}: a float attribute cannot be written")
    w = {"ep": ep_id, "cluster": cat["id"], "attr": a["id"], "c_type": a["c_type"], "zcl_type": a["type"],
         "comp": c["id"],
         "chan": chan, "dp": d["dp"], "dp_type": d["type"], "lo": d["lo"], "hi": d["hi"],
         "num": d["num"], "den": d["den"], "map": None}
    if bmap is not None:
        w["map"] = [(v, k) for k, v in bmap]                 # attribute value -> datapoint value
        return w
    # datapoint count = attribute count * div (milli-units per attribute count) * den / num
    mul, den = div * d["den"], d["num"]
    g = math.gcd(mul, den)
    mul, den = mul // g, den // g
    if mul * den >= 2 ** 31:
        raise ConfigError(f"{where}: the write conversion {mul}/{den} does not fit 32 bits")
    w["mul"], w["den2"] = mul, den
    return w


def _pair_of(cat, a, opts, cname, aname):
    """multiplier and divisor of a measured attribute: the catalog's, or the binding's own."""
    out = []
    for role in ("multiplier", "divisor"):
        pa = cat["attributes"][a["pair"][role]]
        val = opts.get(role, pa["const"])
        if not isinstance(val, int) or isinstance(val, bool) or val < 1:
            raise ConfigError(f"{cname}.{aname}: {role} must be a positive integer, got {val!r}")
        limit = {"UINT16": 0xFFFF, "UINT24": 0xFFFFFF}.get(pa["type"], 0xFFFF)
        if val > limit:
            raise ConfigError(f"{cname}.{aname}: {role} {val} does not fit {pa['type']}")
        out.append(val)
    return out[0], out[1]


def _first_measured(cat):
    for n, a in cat["attributes"].items():
        if "channel" in a:
            return n


def render(cfg: Config, out: Path, base: Path | None = None) -> dict:
    r = resolve(cfg)
    chip, ctx, z = r["chip"], r["ctx"], cfg.zigbee
    out.mkdir(parents=True, exist_ok=True)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(DATA / "templates")),
                             keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=True,
                             undefined=jinja2.StrictUndefined)
    env.filters["hex"] = lambda v: f"0x{int(v):X}"

    # runtime sources
    rt = out / "runtime"
    if rt.exists():
        shutil.rmtree(rt)
    shutil.copytree(DATA / "runtime", rt)

    sleep = chip.d.get("sleep") or {}
    manufacturer_code = z.manufacturer_code or chip.d.get("manufacturer_code")
    kconfig = list(dict.fromkeys(ctx.kconfig))
    kconfig += [f"CONFIG_ZIGBEE_ZCL_REPORTING_TABLE_SIZE={max(len(r['defaults']) + len(r['onoffs']) + 3, 4)}"]
    if manufacturer_code is not None:       # else the SoC's default of the stack
        kconfig += [f"CONFIG_ZIGBEE_MANUFACTURER_CODE=0x{manufacturer_code:X}"]
    kconfig += ["CONFIG_ZIGBEE_OTA=y" if chip.d.get("ota", True) else "CONFIG_ZIGBEE_OTA=n"]
    if chip.d.get("build", {}).get("driver") == "sysbuild":
        kconfig += [f'CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION="{z.version}"']
    kconfig += chip.d.get("kconfig_all", [])
    if z.role == "end_device":
        kconfig += [f"CONFIG_ZIGBEE_ED_POLL_INTERVAL_MS={z.poll_interval * 1000}"]
        if r["sleepy"]:
            kconfig += sleep.get("retained_kconfig", [])
    if not z.debug:
        kconfig += (["CONFIG_CONSOLE=n", "CONFIG_UART_CONSOLE=n"] if ctx.needs_serial
                    else ["CONFIG_SERIAL=n", "CONFIG_CONSOLE=n", "CONFIG_UART_CONSOLE=n"])
    kconfig = [k if k.startswith("CONFIG_") else "CONFIG_" + k for k in kconfig]

    ota_type = z.ota_image_type
    role_conf = ("router" if z.role == "router" else ("end_device" if r["sleepy"] else "end_device_awake"))
    zb_ctx = dict(r, z=z, role_conf=role_conf, ident=ident, ota_type=ota_type, manufacturer_code=manufacturer_code,
                  sleep=sleep, GROUP_MAX=r["group_max"], pwr={"battery": 3, "mains": 1}[r["power"]],
                  comps=ctx.comps, name=z.name, COMMON=COMMON_CLUSTERS,
                  measure_ms=1000 * (z.measure_interval or (z.poll_interval if (r["sleepy"] or z.role == "end_device") else 1)))

    plugin_files = []
    for rel in cfg.plugins:
        src = (base or Path.cwd()) / rel
        if not src.is_file():
            raise ConfigError(f"plugin '{rel}' not found (relative to the YAML: {src})")
        dst = out / "plugins" / Path(rel).name
        dst.parent.mkdir(exist_ok=True)
        shutil.copy(src, dst)
        plugin_files.append(f"plugins/{dst.name}")
    zb_ctx["plugin_files"] = plugin_files

    # devicetree overlay
    base = jinja2.Template((chip.dir / "base.dtsi.j2").read_text()).render(name=z.name, ports=r["ports"],
                                                                         sleepy=r["sleepy"])
    (out / f"{chip.overlay_name}.overlay").write_text(base + "\n" + "\n".join(ctx.overlay) + "\n"
                                               + "\n".join(r["overlay_tail"]))

    (out / "prj.conf").write_text("# Generated by zigbee-builder. Do not edit.\n"
                                  + "\n".join(dict.fromkeys(k if "=" in k else k + "=y" for k in kconfig)) + "\n")
    (out / "version.txt").write_text(z.version + "\n")
    (out / "CMakeLists.txt").write_text(env.get_template("CMakeLists.txt.j2").render(**zb_ctx))
    (out / "zb_user.h").write_text(env.get_template("zb_user.h.j2").render(**zb_ctx))
    (out / "zb_gen.h").write_text(env.get_template("zb_gen.h.j2").render(**zb_ctx))
    (out / "zb_device.c").write_text(env.get_template("zb_device.c.j2").render(**zb_ctx))

    # loader (MCUboot) config of the chip
    wh = chip.d.get("wake_header")
    (out / "chip_wake.h").write_text((chip.dir / wh).read_text() if wh else
        "#pragma once\n#include <stdbool.h>\n#include <zephyr/drivers/gpio.h>\nstatic inline bool zbr_chip_cold_boot(void) { return true; }\nstatic inline void zbr_chip_wake_arm(const struct gpio_dt_spec *s) { (void)s; }\n")
    if chip.d.get("loader"):
        (out / "loader.conf").write_text("\n".join(chip.d["loader"]["conf"]) + "\n")
        if "overlay" in chip.d["loader"]:           # the sysbuild chips only configure MCUboot, their board has its own
            shutil.copy(chip.dir / chip.d["loader"]["overlay"], out / "loader.overlay")

    info = {"name": z.name, "chip": chip.name, "board": chip.board, "snippet": chip.d.get("snippet"),
            "flash": chip.d["flash"], "build": chip.d.get("build", {"driver": "two_step"}),
            "toolchain": chip.d.get("toolchain", "tc32_llvm"), "ota": chip.d.get("ota", True), "version": z.version, "manufacturer_code": manufacturer_code,
            "ota_image_type": ota_type}
    (out / "build_info.json").write_text(json.dumps(info, indent=2) + "\n")
    return info

"""Resolve a Config against the catalogs and render a Zephyr project."""
from __future__ import annotations

import json
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
GROUP_MAX = 4


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
    ctx.comps[c.id] = {
        "id": ident(c.id), "kind": "binary_input", "label": label, "debounce": deb,
        "wake": bool(c.model_extra.get("wake", True)), "chans": {"active": None}, "includes": [],
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
    ctx.comps[c.id] = {
        "id": ident(c.id), "kind": "relay", "label": label, "led": led, "chans": {},
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
         "energy_meter": _kind_energy_meter}


def _type_specs() -> dict:
    specs = {}
    for f in sorted((DATA / "components").glob("*.yaml")):
        specs.update(yaml.safe_load(f.read_text()))
    specs["battery_adc"] = {"kind": "battery_adc"}
    specs["binary_input"] = {"kind": "binary_input"}
    specs["relay"] = {"kind": "relay"}
    specs["pwm_light"] = {"kind": "pwm_light"}
    return specs


def resolve(cfg: Config):
    z = cfg.zigbee
    chip = Chip(z.chip)
    clusters = load_catalog("clusters")
    devices = yaml.safe_load((DATA / "devices.yaml").read_text())
    ctx = Ctx(cfg, chip, clusters)

    if z.role not in chip.d["roles"]:
        raise ConfigError(f"chip {chip.name} does not support role {z.role}")
    can_sleep = chip.d.get("sleep") is not None
    sleepy = (z.role == "end_device" and can_sleep) if z.sleepy is None else z.sleepy
    if sleepy and z.role != "end_device":
        raise ConfigError("sleepy: true needs role: end_device")
    if sleepy and not can_sleep:
        raise ConfigError(f"sleepy: true is not supported on {chip.name}: the stack has no light sleep and "
                          f"the chip pack describes no retained memory for the deep sleep. Use sleepy: false "
                          f"(the end device stays awake) or a router")
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
        if sleepy and not comp["wake"]:
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

    # --- endpoints
    eps, bindings = [], []
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
            for aname, a in cat["attributes"].items():
                if "scale" in ccfg and aname == _first_measured(cat):
                    a = {**a, "scale": float(ccfg["scale"])}
                v = f"{cl['var']}_{aname}"
                at = {"name": aname, "a": a, "var": v, "access": ACCESS[a["access"]]}
                if "const" in a:
                    at["const"] = a["const"]
                    cl["attrs"].append(at)
                    continue
                # attribute-level source wins; the cluster's shorthand "source" is a component
                # (every measured attribute takes its own channel) or component.channel (first one)
                src = ccfg.get(aname)
                short = ccfg.get("source")
                if src is None and short is not None:
                    if "." not in str(short) or aname == _first_measured(cat):
                        src = short
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
                div = round(a["scale"] * 1000)
                if div < 1:
                    raise ConfigError(f"{cname}.{aname}: scale {a['scale']} is finer than the channel unit")
                at.update(init=a.get("invalid", 0), comp=comp, chan=chan, div=div)
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
                if "report" in a or rep:
                    r = {
                        "min": seconds(rep["min"]), "max": seconds(rep["max"]),
                        "change": round(float(rep.get("change", 0)) / a["scale"]),
                    }
                    at["report"] = r
                cl["attrs"].append(at)
                if not a.get("event"):
                    bindings.append(Binding(e.id, cl, aname, at, v, comp, chan, div))
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
    for ep in eps:
        for cl in ep["clusters"]:
            n_rep = sum(1 for a in cl["attrs"] if "report" in a)
            if n_rep > GROUP_MAX:
                raise ConfigError(f"cluster {cl['name']}: more than {GROUP_MAX} reported attributes")

    return {
        "cfg": cfg, "chip": chip, "sleepy": sleepy, "power": power, "eps": eps, "ctx": ctx,
        "bindings": bindings, "defaults": defaults, "inputs": ctx.inputs, "inputs_used": bool(ctx.inputs), "onoffs": ctx.onoffs, "lights": ctx.lights, "service": service, "customs": ctx.customs, "switches": any(ep["switch"] for ep in eps), "ias": ctx.ias, "items": items, "cluster_max": ctx_cluster_max,
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
    startup = _word(ccfg.get("startup", "previous"))
    if startup not in ("off", "on", "previous"):
        raise ConfigError(f"endpoint {e.id}/on_off: startup must be off, on or previous")
    ctx.n_store += 1
    o = {"var": f"ep{e.id}_onoff", "ep": e.id, "relay": comp, "startup": ("off", "on", "previous").index(startup),
         "item": 0xA0 + ctx.n_store - 1, "report": {k: seconds(v) for k, v in cat["report"].items()}}
    comp["onoff_var"] = o["var"]
    ti = comp.get("toggle_input")
    if ti:
        inp = _use_input(ctx, ti, 2, f"component '{comp['id']}': toggle_input")
        inp["users"].append(f"if (active) {{ zbr_onoff_set(&{o['var']}, !{o['var']}.state); }}")
    ctx.onoffs.append(o)
    cl["onoff"] = o


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
            kconfig += sleep["retained_kconfig"]
    if not z.debug:
        kconfig += ["CONFIG_SERIAL=n", "CONFIG_CONSOLE=n", "CONFIG_UART_CONSOLE=n"]
    kconfig = [k if k.startswith("CONFIG_") else "CONFIG_" + k for k in kconfig]

    ota_type = z.ota_image_type
    role_conf = ("router" if z.role == "router" else ("end_device" if r["sleepy"] else "end_device_awake"))
    zb_ctx = dict(r, z=z, role_conf=role_conf, ident=ident, ota_type=ota_type, manufacturer_code=manufacturer_code,
                  sleep=sleep, GROUP_MAX=GROUP_MAX, pwr={"battery": 3, "mains": 1}[r["power"]],
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
    base = jinja2.Template((chip.dir / "base.dtsi.j2").read_text()).render(name=z.name, ports=r["ports"])
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
        shutil.copy(chip.dir / chip.d["loader"]["overlay"], out / "loader.overlay")

    info = {"name": z.name, "chip": chip.name, "board": chip.board, "snippet": chip.d.get("snippet"),
            "flash": chip.d["flash"], "build": chip.d.get("build", {"driver": "two_step"}),
            "toolchain": chip.d.get("toolchain", "tc32_llvm"), "ota": chip.d.get("ota", True), "version": z.version, "manufacturer_code": manufacturer_code,
            "ota_image_type": ota_type}
    (out / "build_info.json").write_text(json.dumps(info, indent=2) + "\n")
    return info

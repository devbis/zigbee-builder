"""Chip packs: everything that depends on the SoC."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

DATA = Path(__file__).parent / "data"


class ConfigError(Exception):
    pass


@dataclass
class Pin:
    name: str          # "PC3"
    node: str          # "gpioc"
    num: int           # 3
    port: int | None = None    # the port number, for the chips that name it (P1.05)


class Chip:
    def __init__(self, name: str):
        self.dir = DATA / "chips" / name
        f = self.dir / "chip.yaml"
        if not f.exists():
            known = sorted(p.name for p in (DATA / "chips").iterdir() if p.is_dir())
            raise ConfigError(f"unknown chip '{name}', known: {', '.join(known)}")
        self.d = yaml.safe_load(f.read_text())
        self.name = self.d["name"]
        self.board = self.d["board"]

    def pin(self, name: str) -> Pin:
        fmt = self.d["pins"]
        if fmt["format"] == "port_letter":
            m = re.fullmatch(r"P([A-Z])(\d+)", str(name).upper())
            if not m or m.group(1) not in fmt["ports"] or int(m.group(2)) >= fmt["per_port"]:
                raise ConfigError(f"pin '{name}' does not exist on {self.name} "
                                  f"(P{fmt['ports'][0]}0..P{fmt['ports'][-1]}{fmt['per_port'] - 1})")
            return Pin(m.group(0), fmt["node"].format(port=m.group(1).lower()), int(m.group(2)))
        if fmt["format"] == "gpio_number":
            m = re.fullmatch(r"GPIO(\d+)", str(name).upper())
            if not m or int(m.group(1)) > fmt["max"]:
                raise ConfigError(f"pin '{name}' does not exist on {self.name} (GPIO0..GPIO{fmt['max']})")
            if int(m.group(1)) in fmt.get("reserved", []):
                raise ConfigError(f"pin '{name}' is reserved on {self.name} (flash or USB); "
                                  f"reserved: {', '.join('GPIO%d' % n for n in fmt['reserved'])}")
            return Pin(m.group(0), fmt["node"], int(m.group(1)))
        if fmt["format"] == "port_dot":
            m = re.fullmatch(r"P(\d+)\.(\d+)", str(name).upper())
            ports = {int(k): v for k, v in fmt["ports"].items()}
            if not m or int(m.group(1)) not in ports or int(m.group(2)) >= ports[int(m.group(1))]:
                raise ConfigError(f"pin '{name}' does not exist on {self.name} "
                                  f"({', '.join('P%d.00..P%d.%02d' % (k, k, v - 1) for k, v in ports.items())})")
            port, num = int(m.group(1)), int(m.group(2))
            canon = f"P{port}.{num:02d}"
            if canon in [str(r).upper() for r in fmt.get("reserved", [])]:
                raise ConfigError(f"pin '{name}' is reserved on {self.name} (NFC or reset); "
                                  f"reserved: {', '.join(fmt['reserved'])}")
            return Pin(f"P{port}.{num}", fmt["node"].format(port=port), num, port)
        raise ConfigError(f"chip {self.name}: unknown pin format")

    @property
    def overlay_name(self) -> str:
        return self.d.get("board_file", self.board.replace("/", "_"))

    def ports(self, pins: list[Pin]) -> list[str]:
        # tlsr8258: the deep-sleep wake works through every port, so all are on
        if self.d["pins"]["format"] == "port_letter" and self.d.get("wake") == "any":
            return [self.d["pins"]["node"].format(port=p.lower()) for p in self.d["pins"]["ports"]
                    if p != "E"]
        return sorted({p.node for p in pins})

    def i2c_hw_for(self, sda: str, scl: str):
        hw = self.d.get("i2c_hw", [])
        if isinstance(hw, dict):            # any pin pair is a hardware bus
            return hw.get("any")
        for e in hw:
            if e["sda"] == sda.upper() and e["scl"] == scl.upper():
                return e
        return None

    def pwm_assign(self, pins: list[str]) -> dict[str, int]:
        """Give every pin a PWM channel of its own, or say why not."""
        table = self.d.get("pwm")
        if not table:
            raise ConfigError(f"chip {self.name} has no PWM description")
        if table.get("assign") == "sequential":     # any pin, the channels of one instance in order
            if len(pins) > table["channels"]:
                raise ConfigError(f"{len(pins)} PWM channels asked, {self.name} gives {table['channels']} "
                                  f"on one instance")
            return {p.upper(): i for i, p in enumerate(pins)}
        opts = {}
        for p in pins:
            o = table["pins"].get(p.upper())
            if not o:
                raise ConfigError(f"pin {p} cannot output PWM on {self.name}; "
                                  f"PWM pins: {', '.join(table['pins'])}")
            opts[p.upper()] = o

        def solve(rest, used):
            if not rest:
                return {}
            pin = rest[0]
            for ch in opts[pin]:
                if ch not in used:
                    sub = solve(rest[1:], used | {ch})
                    if sub is not None:
                        return {pin: ch, **sub}
            return None

        res = solve(sorted(opts, key=lambda k: len(opts[k])), frozenset())
        if res is None:
            raise ConfigError("these pins need the same PWM channel: "
                              + ", ".join(f"{p}: {opts[p]}" for p in opts))
        return res

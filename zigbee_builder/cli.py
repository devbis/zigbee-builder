"""zigbee-builder validate|generate|build"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from pydantic import ValidationError

from .chip import ConfigError
from .config import Config
from .generator import render, resolve


def load(path: str, board: str | None = None) -> Config:
    """The device file, and optionally a board file that gives the chip and the pins instead of its own.
    A board file replaces `pins:` as a whole: its pins are those of another PCB, they do not mix with
    the device file's. `button` and `status_led` of the device file name logical pins and stay, unless
    the board file sets them."""
    try:
        data = yaml.safe_load(Path(path).read_text())
        if board:
            b = yaml.safe_load(Path(board).read_text()) or {}
            extra = set(b) - {"chip", "pins", "button", "status_led"}
            if extra:
                raise ConfigError(f"board file {board}: unknown keys {sorted(extra)} "
                                  f"(a board has chip, pins, button, status_led)")
            if "chip" not in b or "pins" not in b:
                raise ConfigError(f"board file {board}: chip: and pins: are required")
            data["zigbee"]["chip"] = b["chip"]
            data["pins"] = b["pins"]
            for k in ("button", "status_led"):
                if k in b:
                    data[k] = b[k]
        return Config.model_validate(data)
    except ValidationError as e:
        lines = [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
        raise ConfigError("invalid configuration:\n  " + "\n  ".join(lines))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="zigbee-builder")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, h in (("validate", "check the YAML"), ("generate", "write the Zephyr project"),
                    ("build", "generate, build and package")):
        p = sub.add_parser(name, help=h)
        p.add_argument("config")
        p.add_argument("-o", "--out", default="build", help="output directory (default: build)")
        p.add_argument("--board", help="a board file (chip and pins) used instead of the device file's own")
        if name == "build":
            p.add_argument("--env-file", help="sh file setting ZEPHYR_BASE etc.")
    a = ap.parse_args(argv)
    try:
        cfg = load(a.config, a.board)
        if a.cmd == "validate":
            resolve(cfg)
            print("ok")
            return 0
        out = Path(a.out).resolve()      # west runs in the Zephyr tree: paths must not be relative
        project = out / f"{cfg.zigbee.name}-{cfg.zigbee.chip}" / "gen"
        render(cfg, project, Path(a.config).resolve().parent)
        print(f"project: {project}")
        if a.cmd == "build":
            from .build import build
            build(project, out / f"{cfg.zigbee.name}-{cfg.zigbee.chip}", Path(a.env_file).resolve() if a.env_file else None)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

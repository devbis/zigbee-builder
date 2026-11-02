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


def load(path: str) -> Config:
    try:
        return Config.model_validate(yaml.safe_load(Path(path).read_text()))
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
        if name == "build":
            p.add_argument("--env-file", help="sh file setting ZEPHYR_BASE etc.")
    a = ap.parse_args(argv)
    try:
        cfg = load(a.config)
        if a.cmd == "validate":
            resolve(cfg)
            print("ok")
            return 0
        out = Path(a.out)
        project = out / cfg.zigbee.name / "gen"
        render(cfg, project, Path(a.config).resolve().parent)
        print(f"project: {project}")
        if a.cmd == "build":
            from .build import build
            build(project, out / cfg.zigbee.name, Path(a.env_file) if a.env_file else None)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

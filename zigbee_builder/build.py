"""west build of the loader and the application, then signing and OTA packing."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

from .chip import ConfigError


def _env(env_file: Path | None, toolchain: str = "tc32_llvm") -> dict:
    """ZEPHYR_BASE, LLVM_TOOLCHAIN_PATH, ZEPHYR_EXTRA_MODULES, WEST, PYTHON, MCUBOOT_DIR.
    The environment wins over an env file ("export X=" or ': "${X:=...}"' lines of sh)."""
    env = dict(os.environ)
    if env_file and env_file.exists():
        names = ("ZEPHYR_BASE", "LLVM_TOOLCHAIN_PATH", "ZEPHYR_EXTRA_MODULES", "WEST", "PYTHON", "MCUBOOT_DIR",
                 "ZEPHYR_SDK_INSTALL_DIR", "GNUARMEMB_TOOLCHAIN_PATH")
        script = f'. "{env_file}"; ' + "; ".join(f'echo "{n}=${{{n}:-}}"' for n in names)
        out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=True)
        for line in out.stdout.splitlines():
            k, _, v = line.partition("=")
            if v and k not in os.environ:
                env[k] = v
    need = ["ZEPHYR_BASE", "ZEPHYR_EXTRA_MODULES"]
    if toolchain == "tc32_llvm":
        need.append("LLVM_TOOLCHAIN_PATH")
    elif toolchain == "gnuarmemb":
        sdk = env.get("ZEPHYR_SDK_INSTALL_DIR")
        if sdk and (Path(sdk) / "gnu/arm-zephyr-eabi").is_dir():    # the SDK has the ARM toolchain: use it
            env["ZBR_USE_SDK_ARM"] = "1"
            need.append("ZEPHYR_SDK_INSTALL_DIR")
        elif not env.get("GNUARMEMB_TOOLCHAIN_PATH"):   # where arm-none-eabi-gcc is: <path>/bin/arm-none-eabi-gcc
            gcc = shutil.which("arm-none-eabi-gcc", path=env.get("PATH"))
            if gcc:
                env["GNUARMEMB_TOOLCHAIN_PATH"] = str(Path(gcc).parent.parent)   # not resolved: Homebrew links the tools from several formulas
        if not env.get("ZBR_USE_SDK_ARM"):
            need.append("GNUARMEMB_TOOLCHAIN_PATH")
    else:
        if not env.get("ZEPHYR_SDK_INSTALL_DIR"):     # the newest SDK in the home directory
            sdks = sorted(Path.home().glob("zephyr-sdk-*"))
            if sdks:
                env["ZEPHYR_SDK_INSTALL_DIR"] = str(sdks[-1])
        need.append("ZEPHYR_SDK_INSTALL_DIR")
    for k in need:
        if not env.get(k):
            raise ConfigError(f"{k} is not set: export it or pass --env-file (see README)")
    if env.get("WEST") and "/" in env["WEST"]:       # esptool and the rest of west's venv
        env["PATH"] = str(Path(env["WEST"]).parent) + os.pathsep + env.get("PATH", "")
    return env


def build(project: Path, out: Path, env_file: Path | None) -> list[Path]:
    info = json.loads((project / "build_info.json").read_text())
    env = _env(env_file, info["toolchain"])
    west = env.get("WEST", "west")
    if info["build"]["driver"] == "plain":
        return _build_plain(project, out, info, env, west)
    if info["build"]["driver"] == "sysbuild":
        return _build_sysbuild(project, out, info, env, west)
    zb = env["ZEPHYR_BASE"]
    env["ZEPHYR_TOOLCHAIN_VARIANT"] = "host"
    mcuboot = env.get("MCUBOOT_DIR") or subprocess.run(
        [west, "topdir"], cwd=zb, env=env, capture_output=True, text=True, check=True
    ).stdout.strip() + "/bootloader/mcuboot"
    env["MCUBOOT_DIR"] = mcuboot
    tc = ["-DTOOLCHAIN_VARIANT_COMPILER=llvm", f"-DLLVM_TOOLCHAIN_PATH={env['LLVM_TOOLCHAIN_PATH']}"]

    loader = out / f"loader-{info['name']}"
    app = out / info["name"]
    run = lambda cmd: subprocess.run(cmd, cwd=zb, env=env, check=True)
    run([west, "build", "-p", "auto", "-b", info["board"], "-d", str(loader),
         f"{mcuboot}/boot/zephyr", "--", *tc,
         f"-DEXTRA_CONF_FILE={project / 'loader.conf'}",
         f"-DEXTRA_DTC_OVERLAY_FILE={project / 'loader.overlay'}"])
    run([west, "build", "-p", "always", "-b", info["board"], "-d", str(app), str(project), "--",
         f"-DZEPHYR_EXTRA_MODULES={env['ZEPHYR_EXTRA_MODULES']}",
         f"-DAPP_BUILD_DATE={os.environ.get('APP_BUILD_DATE') or date.today().strftime('%Y%m%d')}",
         *tc])
    return package(project, app, loader / "zephyr/zephyr.bin", env)


def _variant(info: dict, env: dict) -> str:
    return "gnuarmemb" if info["toolchain"] == "gnuarmemb" and not env.get("ZBR_USE_SDK_ARM") else "zephyr"


def _build_plain(project: Path, out: Path, info: dict, env: dict, west: str) -> list[Path]:
    """One west build: the image is flashed as it is, at the offset the chip pack names."""
    zb = env["ZEPHYR_BASE"]
    env["ZEPHYR_TOOLCHAIN_VARIANT"] = _variant(info, env)
    app = out / info["name"]
    subprocess.run(
        [west, "build", "-p", "always", "-b", info["board"], "-d", str(app), str(project), "--",
         f"-DZEPHYR_EXTRA_MODULES={env['ZEPHYR_EXTRA_MODULES']}",
         f"-DAPP_BUILD_DATE={os.environ.get('APP_BUILD_DATE') or date.today().strftime('%Y%m%d')}"],
        cwd=zb, env=env, check=True)
    full = app / f"{info['name']}-{info['version']}-full.bin"
    full.write_bytes((app / "zephyr" / info["build"].get("image", "zephyr.bin")).read_bytes())
    print(f"{full.name}: {full.stat().st_size} bytes, flash at {info['flash']['full_image_offset']:#x}"
          f"{'' if info['ota'] else ' (no OTA on this chip yet)'}")
    return [full]


def _build_sysbuild(project: Path, out: Path, info: dict, env: dict, west: str) -> list[Path]:
    """One west build --sysbuild with the Zephyr SDK: MCUboot and the signed application image.
    The full image is the loader, padded to the slot, and the signed image."""
    env["ZEPHYR_TOOLCHAIN_VARIANT"] = _variant(info, env)
    build = out / info["name"]
    subprocess.run(
        [west, "build", "--sysbuild", "-p", "always", "-b", info["board"], "-d", str(build), str(project), "--",
         "-DSB_CONFIG_BOOTLOADER_MCUBOOT=y", *[f"-D{c}" for c in info["build"].get("sb_config", [])],
         f"-DZEPHYR_EXTRA_MODULES={env['ZEPHYR_EXTRA_MODULES']}",
         f"-DAPP_BUILD_DATE={os.environ.get('APP_BUILD_DATE') or date.today().strftime('%Y%m%d')}"],
        cwd=env["ZEPHYR_BASE"], env=env, check=True)
    app = build / project.name                       # sysbuild names the application by its directory
    return _package(info, app, build / "mcuboot/zephyr" / info["build"].get("loader_image", "zephyr.bin"), app / "zephyr/zephyr.signed.bin", env)


def _package(info: dict, app: Path, loader_bin: Path, signed: Path, env: dict) -> list[Path]:
    """OTA file and full image from an image that is already signed."""
    fl = info["flash"]
    python = env.get("PYTHON") or sys.executable
    ota_pack = Path(env["ZEPHYR_BASE"]) / "scripts/zigbee_ota_pack.py"
    stem = app.parent / f"{info['name']}-{info['version']}"
    ota, full, sig = Path(f"{stem}.zigbee"), Path(f"{stem}-full.bin"), Path(f"{stem}-signed.bin")
    sig.write_bytes(signed.read_bytes())
    if info["ota"]:
        oi = dict(l.split("=", 1) for l in (app / "ota_info.txt").read_text().split() if "=" in l)
        manufacturer = _kconfig((app / "zephyr/.config").read_text(), "CONFIG_ZIGBEE_MANUFACTURER_CODE")
        subprocess.run([python, str(ota_pack), str(sig), str(ota), "--manufacturer", hex(manufacturer),
                        "--image-type", hex(int(oi["image_type"], 0)), "--file-version",
                        hex(int(oi["file_version"], 0)), "--header-string", info["name"][:32]], check=True)
    boot = loader_bin.read_bytes()
    start = fl.get("loader_offset", 0)
    if start + len(boot) > fl["loader_end"]:
        raise ConfigError(f"loader is {len(boot):#x} bytes, its partition ends at {fl['loader_end']:#x}")
    image = sig.read_bytes()
    if len(image) > fl["slot_size"]:
        raise ConfigError(f"image is {len(image):#x} bytes, slot 0 holds {fl['slot_size']:#x}")
    if fl.get("boot_header") == "tool":
        # the boot ROM's header comes from the flashing tool: the loader and the image go in separately
        loader = Path(f"{stem}-loader.bin")
        loader.write_bytes(boot)
        print(f"{sig.name}: {len(image)} bytes (slot {fl['slot_size']:#x}), flash at {fl['slot_offset']:#x}")
        print(f"{loader.name}: {len(boot)} bytes, flash with bflb_mcu_tool (it adds the boot header, at "
              f"{start:#x}); no full image")
        return [sig, loader] + ([ota] if info["ota"] else [])
    full.write_bytes(boot.ljust(fl["loader_end"] - start, b"\xff") + image)
    print(f"{sig.name}: {len(image)} bytes (slot {fl['slot_size']:#x})")
    print(f"{full.name}: loader {len(boot)} + image {len(image)} bytes, flash at {start:#x}")
    return [sig, full] + ([ota] if info["ota"] else [])


def _kconfig(text: str, name: str) -> int:
    m = re.search(rf"^{name}=(0x[0-9A-Fa-f]+|\d+)$", text, re.M)
    if not m:
        raise ConfigError(f"{name} not found in the build's .config")
    return int(m.group(1), 0)


def package(project: Path, app: Path, loader_bin: Path, env: dict) -> list[Path]:
    info = json.loads((project / "build_info.json").read_text())
    fl = info["flash"]
    python = env.get("PYTHON") or sys.executable
    imgtool = Path(env["MCUBOOT_DIR"]) / "scripts/imgtool.py"
    ota_pack = Path(env["ZEPHYR_BASE"]) / "scripts/zigbee_ota_pack.py"
    version = info["version"]
    oi = dict(l.split("=", 1) for l in (app / "ota_info.txt").read_text().split() if "=" in l)
    manufacturer = _kconfig((app / "zephyr/.config").read_text(), "CONFIG_ZIGBEE_MANUFACTURER_CODE")

    stem = app / f"{info['name']}-{version}"
    signed, ota, full = Path(f"{stem}-signed.bin"), Path(f"{stem}.zigbee"), Path(f"{stem}-full.bin")
    subprocess.run([python, str(imgtool), "sign", "--header-size", hex(fl["header_size"]), "--pad-header",
                    "--align", "4", "--slot-size", hex(fl["slot_size"]), "--version", version,
                    str(app / "zephyr/zephyr.bin"), str(signed)], check=True)
    subprocess.run([python, str(ota_pack), str(signed), str(ota), "--manufacturer", hex(manufacturer),
                    "--image-type", hex(int(oi["image_type"], 0)), "--file-version",
                    hex(int(oi["file_version"], 0)), "--header-string", info["name"][:32]], check=True)
    boot = loader_bin.read_bytes()
    if len(boot) > fl["loader_end"]:
        raise ConfigError(f"loader is {len(boot):#x} bytes, its partition ends at {fl['loader_end']:#x}")
    image = signed.read_bytes()
    full.write_bytes(boot.ljust(fl["loader_end"], b"\xff") + image)
    print(f"{signed.name}: {len(image)} bytes (slot {fl['slot_size']:#x})")
    print(f"{full.name}: loader {len(boot)} + image {len(image)} bytes")
    return [signed, ota, full]

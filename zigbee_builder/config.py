"""The YAML schema. Strict on the structure, components stay free-form until resolved."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .units import seconds


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ZigbeeCfg(Strict):
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    manufacturer: str = Field(max_length=32)
    model: str = Field(max_length=32)
    chip: str
    role: Literal["router", "end_device"] = "end_device"
    # sleepy: only for end devices; default on
    sleepy: bool | None = None
    poll_interval: int = 60          # seconds between wakes of a sleepy device
    measure_interval: int | None = None   # awake devices: seconds between measurements (default 1)
    power: Literal["battery", "mains"] | None = None
    manufacturer_code: int | None = None
    ota_image_type: int = 0xFFFF
    version: str = "0.1.0"
    # the chip pack's debug console etc.
    debug: bool = False

    @field_validator("poll_interval", "measure_interval", mode="before")
    @classmethod
    def _p(cls, v):
        return None if v is None else seconds(v)

    @field_validator("version")
    @classmethod
    def _v(cls, v):
        parts = v.split(".")
        if len(parts) != 3 or not all(x.isdigit() for x in parts):
            raise ValueError("version must be major.minor.build")
        if int(parts[0]) > 15 or int(parts[1]) > 15 or int(parts[2]) > 255:
            raise ValueError("version: major and minor at most 15, build at most 255")
        return v


class PinCfg(Strict):
    pin: str
    pull: Literal["up", "down", "none"] = "none"
    inverted: bool = False


class I2cCfg(Strict):
    id: str = "default"
    sda: str
    scl: str
    hw: bool | Literal["auto"] = "auto"
    frequency: Literal["standard", "fast"] = "standard"


class ComponentCfg(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    type: str


class CustomAttr(Strict):
    id: int
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: Literal["bool", "uint8", "uint16", "uint32", "int8", "int16", "int32", "enum8",
                  "bitmap8", "bitmap16"]
    access: Literal["r", "rw"] = "r"
    default: int | bool = 0
    report: dict[str, Any] | None = None
    persist: bool = False
    invalid: int | None = None     # a value that means "not measured": never reported


class CustomCommand(Strict):
    id: int = Field(ge=0, le=255)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")


class CustomCluster(Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    id: int
    manufacturer_code: int = 0
    attributes: list[CustomAttr] = []
    commands: list[CustomCommand] = []   # received; handled by the plugin's on_command


class EndpointCfg(Strict):
    id: int = Field(ge=1, le=240)
    device: str = "custom"
    version: int = 1
    # cluster name -> {attribute|source|report ...}
    clusters: dict[str, dict[str, Any] | None] = {}


class Config(Strict):
    zigbee: ZigbeeCfg
    pins: dict[str, PinCfg | str] = {}
    i2c: list[I2cCfg] | I2cCfg | None = None
    components: list[ComponentCfg] = []
    endpoints: list[EndpointCfg]
    custom_clusters: list[CustomCluster] = []
    plugins: list[str] = []            # C files, relative to the YAML
    button: str | None = None          # pin name from pins: -> sw0
    status_led: str | None = None      # pin name from pins: -> led0

    def pin_cfg(self, ref: str) -> PinCfg:
        p = self.pins.get(ref)
        if p is None:
            return PinCfg(pin=ref)
        return PinCfg(pin=p) if isinstance(p, str) else p

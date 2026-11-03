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


class ServiceCfg(Strict):
    """The buttons that join and reset the device when held: see zbr_service.c."""
    hold: int = 5                      # seconds
    search_time: int = 60              # seconds the search window stays open
    search_on_power_on: bool = True    # search for a network when the power is applied and there is none
    inputs: Literal["any"] | list[str] = []

    @field_validator("hold", "search_time", mode="before")
    @classmethod
    def _t(cls, v):
        return seconds(v)

    @field_validator("hold")
    @classmethod
    def _hold(cls, v):
        if not 3 <= v <= 10:
            raise ValueError("hold: 3 to 10 seconds")
        return v

    @field_validator("search_time")
    @classmethod
    def _search(cls, v):
        if not 10 <= v <= 900:
            raise ValueError("search_time: 10 to 900 seconds")
        return v


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
    button: str | None = None          # a pin of pins:, the service button (short for service: inputs: [it])
    service: ServiceCfg | None = None
    status_led: str | None = None      # pin name from pins: -> led0

    def pin_cfg(self, ref: str) -> PinCfg:
        p = self.pins.get(ref)
        return PinCfg(pin=ref) if p is None else (PinCfg(pin=p) if isinstance(p, str) else p)

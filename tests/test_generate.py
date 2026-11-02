import copy
from pathlib import Path

import pytest
import yaml

from zigbee_builder.chip import ConfigError
from zigbee_builder.cli import load
from zigbee_builder.config import Config
from zigbee_builder.generator import render, resolve

EX = Path(__file__).parent.parent / "examples"


def cfg(**patch):
    d = yaml.safe_load((EX / "temp-humidity-sht30.yaml").read_text())
    for k, v in patch.items():
        d[k] = v
    return Config.model_validate(d)


def test_example_renders(tmp_path):
    render(load(str(EX / "temp-humidity-sht30.yaml")), tmp_path)
    c = (tmp_path / "zb_device.c").read_text()
    assert "ZCL_CLUSTER_MS_TEMPERATURE_MEASUREMENT" in c and "zbr_report_all" in c
    assert "CONFIG_TLSR8258_PM_DEEP_RETENTION=y" in (tmp_path / "prj.conf").read_text()


def test_pin_change_moves_only_overlay(tmp_path):
    d = yaml.safe_load((EX / "temp-humidity-sht30.yaml").read_text())
    d["pins"]["button"]["pin"] = "PA4"
    render(Config.model_validate(d), tmp_path)
    assert "&gpioa 4" in (tmp_path / "tlsr8258_generic.overlay").read_text()


def test_soft_i2c_when_no_hw_pair(tmp_path):
    d = yaml.safe_load((EX / "temp-humidity-sht30.yaml").read_text())
    d["pins"]["sda"], d["pins"]["scl"] = "PC3", "PC4"
    render(Config.model_validate(d), tmp_path)
    assert "gpio-i2c" in (tmp_path / "tlsr8258_generic.overlay").read_text()


@pytest.mark.parametrize("patch,msg", [
    ({"pins": {"sda": "PZ9", "scl": "PC3"}}, "does not exist"),
    ({"pins": {"sda": "PC2", "scl": "PC2"}}, "used twice"),
])
def test_errors(patch, msg):
    with pytest.raises(ConfigError, match=msg):
        resolve(cfg(**patch))


def test_unknown_source():
    d = yaml.safe_load((EX / "temp-humidity-sht30.yaml").read_text())
    d["endpoints"][0]["clusters"]["temperature"] = {"source": "nope.temperature"}
    with pytest.raises(ConfigError, match="no component"):
        resolve(Config.model_validate(d))


def ias(name="door-sensor.yaml"):
    return yaml.safe_load((EX / name).read_text())


def test_ias_pin_change_moves_only_overlay(tmp_path):
    d = ias()
    d["pins"]["reed"]["pin"] = "PD3"
    render(Config.model_validate(d), tmp_path)
    assert "&gpiod 3" in (tmp_path / "tlsr8258_generic.overlay").read_text()
    c = (tmp_path / "zb_device.c").read_text()
    assert "zbr_ias_set(&ep1_ias, 0x0001, active)" in c and ".zone_type = 0x15" in c


def test_ias_zone_types(tmp_path):
    render(Config.model_validate(ias("leak-sensor.yaml")), tmp_path)
    assert ".zone_type = 0x2A" in (tmp_path / "zb_device.c").read_text()
    d = ias()
    d["endpoints"][0]["clusters"]["ias_zone"]["zone_type"] = "nope"
    with pytest.raises(ConfigError, match="zone_type"):
        resolve(Config.model_validate(d))


def test_ias_needs_binary_input():
    d = ias()
    d["endpoints"][0]["clusters"]["ias_zone"]["source"] = "bat"
    with pytest.raises(ConfigError, match="not a binary_input"):
        resolve(Config.model_validate(d))


def test_poll_interval_limit():
    d = ias()
    d["zigbee"]["poll_interval"] = "5min"
    with pytest.raises(ConfigError, match="poll_interval"):
        resolve(Config.model_validate(d))


def plug():
    return yaml.safe_load((EX / "plug-bl0937.yaml").read_text())


def test_plug_renders(tmp_path):
    render(Config.model_validate(plug()), tmp_path)
    c = (tmp_path / "zb_device.c").read_text()
    assert "zbr_onoff_cmd" in c and "ZCL_CLUSTER_SE_METERING" in c and "IOTDRV_SENSOR_ATTR_ENERGY_TOTAL" in c
    ov = (tmp_path / "tlsr8258_generic.overlay").read_text()
    assert 'compatible = "belling,bl0937"' in ov and "&gpiod 2" in ov
    # a router never sleeps: no retained-RAM reporting items
    assert "report_items" not in c


def test_relay_pin_change_moves_only_overlay(tmp_path):
    d = plug()
    d["pins"]["relay"] = "PC4"
    render(Config.model_validate(d), tmp_path)
    assert "&gpioc 4" in (tmp_path / "tlsr8258_generic.overlay").read_text()


def test_on_off_needs_relay():
    d = plug()
    d["endpoints"][0]["clusters"]["on_off"]["source"] = "meter"
    with pytest.raises(ConfigError, match="not a relay"):
        resolve(Config.model_validate(d))


def test_meter_needs_pins():
    d = plug()
    del d["components"][1]["sel"]
    with pytest.raises(ConfigError, match="sel"):
        resolve(Config.model_validate(d))


def test_plug_without_metering(tmp_path):
    render(Config.model_validate(yaml.safe_load((EX / "plug-relay.yaml").read_text())), tmp_path)
    assert "METERING" not in (tmp_path / "prj.conf").read_text()


def test_battery_switch(tmp_path):
    render(load(str(EX / "battery-switch.yaml")), tmp_path)
    c = (tmp_path / "zb_device.c").read_text()
    assert "zbr_switch_send(1, ZBR_SWITCH_TOGGLE)" in c and "zbr_switch_send(2, ZBR_SWITCH_TOGGLE)" in c
    # a held button at a wake is a press; a boot with it released is nothing
    assert ".initial = 1" in c
    assert "ZCL_CLUSTER_GEN_ON_OFF }" in c or "ZCL_CLUSTER_GEN_ON_OFF," in c


def test_switch_release_action(tmp_path):
    d = yaml.safe_load((EX / "battery-switch.yaml").read_text())
    d["endpoints"][0]["clusters"]["on_off_switch"]["release_action"] = "off"
    d["endpoints"][0]["clusters"]["on_off_switch"]["action"] = "on"
    render(Config.model_validate(d), tmp_path)
    c = (tmp_path / "zb_device.c").read_text()
    assert "zbr_switch_send(1, ZBR_SWITCH_ON)" in c and "zbr_switch_send(1, ZBR_SWITCH_OFF)" in c


def test_switch_sleepy_needs_wake():
    d = yaml.safe_load((EX / "battery-switch.yaml").read_text())
    d["components"][0]["wake"] = False
    with pytest.raises(ConfigError, match="wake"):
        resolve(Config.model_validate(d))


def test_multi_endpoint_relays(tmp_path):
    render(load(str(EX / "relay-3ch.yaml")), tmp_path)
    c = (tmp_path / "zb_device.c").read_text()
    for n in (1, 2, 3):
        assert f"ep{n}_onoff" in c
    assert c.count("zbr_onoff_set(&ep1_onoff") == 1       # only its own button
    assert "ep4_desc" in c and ".app_in_cluster_count = 0U" in c


def test_cluster_table_grows_with_endpoints(tmp_path):
    d = yaml.safe_load((EX / "relay-3ch.yaml").read_text())
    d["pins"]["r4"] = "PD5"
    d["components"].append({"id": "lamp4", "type": "relay", "pin": "r4"})
    d["endpoints"].append({"id": 5, "device": "on_off_output",
                           "clusters": {"groups": None, "on_off": {"source": "lamp4"}}})
    render(Config.model_validate(d), tmp_path)
    assert "ZCL_CLUSTER_NUM_MAX=10" in (tmp_path / "CMakeLists.txt").read_text()


def test_bare_on_off_words(tmp_path):
    """`action: on` is a boolean to YAML 1.1; it must still mean the word."""
    d = yaml.safe_load("action: on\nrelease_action: off\nstartup: off\n")
    assert d == {"action": True, "release_action": False, "startup": False}
    p = yaml.safe_load((EX / "plug-relay.yaml").read_text())
    p["endpoints"][0]["clusters"]["on_off"]["startup"] = False
    render(Config.model_validate(p), tmp_path)
    assert ".startup = 0" in (tmp_path / "zb_device.c").read_text()


ALL = sorted(p.name for p in EX.glob("*.yaml"))


@pytest.mark.parametrize("name", ALL)
def test_every_example_renders(name, tmp_path):
    render(load(str(EX / name)), tmp_path, EX)
    assert (tmp_path / "zb_device.c").read_text() and (tmp_path / "prj.conf").exists()


def test_custom_cluster(tmp_path):
    render(load(str(EX / "custom-cluster.yaml")), tmp_path, EX)
    h = (tmp_path / "zb_user.h").read_text()
    assert "zb_heater_set_runtime_s(uint32_t value)" in h and "ZB_HEATER_CMD_RESET 0" in h
    c = (tmp_path / "zb_device.c").read_text()
    assert "heater_save()" in c and ".manuCode = 4660" in c
    assert (tmp_path / "plugins" / "heater.c").exists()
    assert "plugins/heater.c" in (tmp_path / "CMakeLists.txt").read_text()


def test_custom_cluster_errors(tmp_path):
    d = yaml.safe_load((EX / "custom-cluster.yaml").read_text())
    d["custom_clusters"][0]["attributes"][1]["id"] = 0
    with pytest.raises(ConfigError, match="twice"):
        resolve(Config.model_validate(d))
    d = yaml.safe_load((EX / "custom-cluster.yaml").read_text())
    d["plugins"] = ["plugins/missing.c"]
    with pytest.raises(ConfigError, match="not found"):
        render(Config.model_validate(d), tmp_path, EX)


def test_sleepy_abstract_reports(tmp_path):
    render(load(str(EX / "abstract-sensor.yaml")), tmp_path, EX)
    c = (tmp_path / "zb_device.c").read_text()
    assert "zb_schedule_report();" in c and "report_items" in c
    assert "zbr_float_bits(bat_voltage, 1000U)" in c      # volts: no float arithmetic on tc32


def light():
    return yaml.safe_load((EX / "light-rgbww.yaml").read_text())


def test_light_renders(tmp_path):
    render(Config.model_validate(light()), tmp_path)
    c = (tmp_path / "zb_device.c").read_text()
    assert "zbr_light_level_cmd" in c and "zbr_light_color_cmd" in c and "[ZBR_LIGHT_WARM]" in c
    ov = (tmp_path / "tlsr8258_generic.overlay").read_text()
    # every pin got a channel of its own: PB4 -> 4, PB5 -> 5, PC2 -> 0, PC3 -> 1
    for pin, ch in (("PB4", 4), ("PB5", 5), ("PC2", 0), ("PC3", 1)):
        assert f"TLSR8258_GPIO_{pin}, TLSR8258_FUNC_PWM{ch}" in ov
    conf = (tmp_path / "prj.conf").read_text()
    assert "CONFIG_ZIGBEE_ZCL_LIGHT_COLOR_CONTROL=y" in conf and "CONFIG_PWM=y" in conf


def test_light_pwm_pin_errors():
    d = light()
    d["pins"]["r"] = "PA5"                    # no PWM on this pin
    with pytest.raises(ConfigError, match="cannot output PWM"):
        resolve(Config.model_validate(d))
    d = light()
    d["pins"]["g"] = "PB1"
    d["pins"]["r"] = "PB4"                    # both want channel 4
    with pytest.raises(ConfigError, match="same PWM channel"):
        resolve(Config.model_validate(d))


def test_light_needs_colour_channels_for_color_control():
    d = light()
    for k in ("red", "green", "blue", "warm"):
        d["components"][0].pop(k, None)
    d["components"][0]["warm"] = "w"
    with pytest.raises(ConfigError, match="no RGB or warm"):
        resolve(Config.model_validate(d))


def test_light_one_endpoint():
    d = light()
    d["endpoints"].append({"id": 2, "device": "dimmable_light", "clusters": {"level": {"source": "lamp"}}})
    with pytest.raises(ConfigError, match="one endpoint"):
        resolve(Config.model_validate(d))

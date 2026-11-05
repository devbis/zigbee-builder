import json
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


PORTABLE = ["plug-relay", "plug-bl0937", "relay-3ch", "custom-cluster"]


@pytest.mark.parametrize("name", PORTABLE)
def test_device_file_with_board_file(name, tmp_path):
    """A board file swaps the chip and the pins; the device file is untouched."""
    cfg = load(str(EX / f"{name}.yaml"), str(EX / "boards/esp32h2" / f"{name}.yaml"))
    assert cfg.zigbee.chip == "esp32h2"
    render(cfg, tmp_path, EX)
    assert "&gpio0 " in (tmp_path / "esp32h2_devkitm_esp32h2.overlay").read_text()


def test_board_file_replaces_pins_as_a_whole():
    cfg = load(str(EX / "plug-relay.yaml"), str(EX / "boards/esp32h2/plug-relay.yaml"))
    assert cfg.pins["relay"] == "GPIO12" and cfg.pin_cfg("button").pin == "GPIO9"


def test_board_file_errors(tmp_path):
    f = tmp_path / "b.yaml"
    f.write_text("chip: esp32h2\n")
    with pytest.raises(ConfigError, match="chip: and pins: are required"):
        load(str(EX / "plug-relay.yaml"), str(f))
    f.write_text("chip: esp32h2\npins: {}\nfoo: 1\n")
    with pytest.raises(ConfigError, match="unknown keys"):
        load(str(EX / "plug-relay.yaml"), str(f))


def test_esp32h2_differences(tmp_path):
    cfg = load(str(EX / "plug-relay.yaml"), str(EX / "boards/esp32h2/plug-relay.yaml"))
    render(cfg, tmp_path, EX)
    conf = (tmp_path / "prj.conf").read_text()
    assert "CONFIG_ZIGBEE_OTA=y" in conf and "TLSR" not in conf and "MINIMAL_LIBC" not in conf
    # the image is signed by sysbuild: its version comes from the YAML
    assert 'CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION="0.1.0"' in conf
    assert not (tmp_path / "loader.conf").exists()          # MCUboot is built by sysbuild, not by us
    assert "SNIPPET" not in (tmp_path / "CMakeLists.txt").read_text()
    # no led0 on the devkit: the overlay makes one
    assert "aliases { led0 = &status_led; }" in (tmp_path / "esp32h2_devkitm_esp32h2.overlay").read_text()


def test_esp32h2_end_device_stays_awake(tmp_path):
    cfg = load(str(EX / "plug-relay.yaml"), str(EX / "boards/esp32h2/plug-relay.yaml"))
    d = cfg.model_dump()
    d["zigbee"].update(role="end_device")
    render(Config.model_validate(d), tmp_path, EX)
    assert "end_device_awake.conf" in (tmp_path / "CMakeLists.txt").read_text()
    d["zigbee"]["sleepy"] = True
    with pytest.raises(ConfigError, match="not supported on esp32h2"):
        resolve(Config.model_validate(d))


def test_esp32h2_pins():
    d = load(str(EX / "plug-relay.yaml"), str(EX / "boards/esp32h2/plug-relay.yaml")).model_dump()
    d["pins"]["relay"] = "GPIO16"                                      # a flash pin
    with pytest.raises(ConfigError, match="reserved"):
        resolve(Config.model_validate(d))
    d["pins"]["relay"] = "PD2"                                         # a TLSR pin name
    with pytest.raises(ConfigError, match="does not exist on esp32h2"):
        resolve(Config.model_validate(d))


def test_esp32h2_has_no_battery_adc_pwm():
    for name, msg in (("temp-humidity-sht30.yaml", "does not exist"), ("light-rgbww.yaml", "does not exist")):
        d = yaml.safe_load((EX / name).read_text())
        d["zigbee"]["chip"] = "esp32h2"                              # the TLSR pin names do not exist there
        with pytest.raises(ConfigError, match=msg):
            resolve(Config.model_validate(d))


NRF = sorted(p.stem for p in (EX / "boards/nrf52840").glob("*.yaml"))


def nrf(name):
    return load(str(EX / f"{name}.yaml"), str(EX / "boards/nrf52840" / f"{name}.yaml"))


@pytest.mark.parametrize("name", NRF)
def test_every_example_on_nrf52840(name, tmp_path):
    cfg = nrf(name)
    assert cfg.zigbee.chip == "nrf52840"
    render(cfg, tmp_path, EX)
    # the name of the overlay is the board's, qualifiers joined with an underscore
    assert (tmp_path / "nrf52840dk_nrf52840.overlay").exists()
    conf = (tmp_path / "prj.conf").read_text()
    assert "CONFIG_IEEE802154_NRF5=y" in conf and "CONFIG_ZIGBEE_STACK_SIZE=4096" in conf


def test_nrf52840_peripherals(tmp_path):
    render(nrf("temp-humidity-sht30"), tmp_path, EX)
    ov = (tmp_path / "nrf52840dk_nrf52840.overlay").read_text()
    assert "NRF_PSEL(TWIM_SDA, 0, 26)" in ov and "NRF_PSEL(TWIM_SCL, 0, 27)" in ov
    assert "zephyr,input-positive = <NRF_SAADC_VDD>" in ov
    # the SAADC gives counts, not millivolts like the TLSR's VBAT mode
    assert "adc_raw_to_millivolts_dt" in (tmp_path / "zb_device.c").read_text()
    t = tmp_path / "tlsr"
    render(load(str(EX / "temp-humidity-sht30.yaml")), t, EX)
    assert "adc_raw_to_millivolts_dt" not in (t / "zb_device.c").read_text()


def test_nrf52840_light_channels(tmp_path):
    render(nrf("light-rgbww"), tmp_path, EX)
    ov = (tmp_path / "nrf52840dk_nrf52840.overlay").read_text()
    for i, pin in enumerate((1, 2, 3, 4)):
        assert f"NRF_PSEL(PWM_OUT{i}, 1, {pin})" in ov
    d = nrf("light-rgbww").model_dump()
    d["pins"]["c"] = "P1.06"
    d["components"][0]["cool"] = "c"
    with pytest.raises(ConfigError, match="5 PWM channels asked"):
        resolve(Config.model_validate(d))


def test_nrf52840_pins():
    d = nrf("plug-relay").model_dump()
    for bad, msg in (("P0.09", "reserved"), ("P0.18", "reserved"), ("P2.00", "does not exist"),
                     ("P0.32", "does not exist"), ("PD2", "does not exist")):
        d["pins"]["relay"] = bad
        with pytest.raises(ConfigError, match=msg):
            resolve(Config.model_validate(d))
    d["pins"]["relay"] = "P0.5"                 # the same pin as P0.05
    d["pins"]["led"] = {"pin": "P0.05"}
    with pytest.raises(ConfigError, match="used twice"):
        resolve(Config.model_validate(d))


def test_nrf52840_no_sleepy():
    d = nrf("temp-humidity-sht30").model_dump()
    d["zigbee"]["sleepy"] = True
    with pytest.raises(ConfigError, match="not supported on nrf52840"):
        resolve(Config.model_validate(d))


def wall():
    return yaml.safe_load((EX / "wall-switch-4.yaml").read_text())


def holds(tmp_path, d):
    """The inputs of the generated table that watch a long press."""
    render(Config.model_validate(d), tmp_path, EX)
    c = (tmp_path / "zb_device.c").read_text()
    return [n for n in ("k1", "k2", "k3", "k4", "rst", "svc_button")
            if f"DT_NODELABEL(in_{n})" in c and
            ".on_hold = zbr_service_hold" in c.split(f"DT_NODELABEL(in_{n})")[1].split("},")[0]]


def test_service_any_key(tmp_path):
    assert holds(tmp_path, wall()) == ["k1", "k2", "k3", "k4"]


def test_service_chosen_keys(tmp_path):
    d = wall()
    d["service"]["inputs"] = ["k1", "k4"]
    assert holds(tmp_path, d) == ["k1", "k4"]


def test_service_recessed_button(tmp_path):
    d = wall()
    d["pins"]["pr"] = {"pin": "PC0", "pull": "up", "inverted": True}
    d["components"].append({"id": "rst", "type": "binary_input", "pin": "pr"})
    d["service"]["inputs"] = ["rst"]
    assert holds(tmp_path, d) == ["rst"]           # no endpoint uses it: it does nothing else


def test_button_is_a_service_input(tmp_path):
    cfg = load(str(EX / "plug-relay.yaml"))
    render(cfg, tmp_path, EX)
    c = (tmp_path / "zb_device.c").read_text()
    assert ".on_hold = zbr_service_hold" in c and ".hold_ms = 5000" in c
    # one search window of 60 s, not open at boot
    g = (tmp_path / "zb_gen.h").read_text()
    assert "ZBR_SEARCH_MS 60000U" in g and "ZBR_SEARCH_ON_BOOT" not in g
    # ... but when the power is applied to a device with no network
    assert "ZBR_SEARCH_ON_POWER_ON 1" in g
    # the button no longer is sw0: the board's own sw0 must not start a factory reset
    assert "app_buttons_init" not in c and "sw0" not in (tmp_path / "tlsr8258_generic.overlay").read_text()


def test_button_and_service_merge(tmp_path):
    d = wall()
    d["pins"]["pb"] = {"pin": "PC0", "pull": "up", "inverted": True}
    d["button"] = "pb"
    d["service"].update(inputs=["k2"], hold="3s", search_time="2min")
    assert holds(tmp_path, d) == ["k2", "svc_button"]
    g = (tmp_path / "zb_gen.h").read_text()
    assert "ZBR_SEARCH_MS 120000U" in g
    assert ".hold_ms = 3000" in (tmp_path / "zb_device.c").read_text()


def test_no_service_input_searches_at_boot(tmp_path):
    d = wall()
    del d["service"]
    render(Config.model_validate(d), tmp_path, EX)
    g = (tmp_path / "zb_gen.h").read_text()
    assert "ZBR_SEARCH_ON_BOOT 1" in g and "ZBR_SEARCH_MS 0U" in g
    assert ".on_hold" not in (tmp_path / "zb_device.c").read_text()


def test_service_errors():
    d = wall()
    d["service"]["inputs"] = ["nope"]
    with pytest.raises(ConfigError, match="not a binary_input"):
        resolve(Config.model_validate(d))
    d = wall()
    d["service"]["inputs"] = ["bat"]
    with pytest.raises(ConfigError, match="not a binary_input"):
        resolve(Config.model_validate(d))
    for key, val in (("hold", "2s"), ("hold", "20s"), ("search_time", "5s"), ("search_time", "20min")):
        d = wall()
        d["service"][key] = val
        with pytest.raises(Exception, match="seconds"):
            Config.model_validate(d)
    d = wall()
    d["components"][0]["wake"] = False
    with pytest.raises(ConfigError, match="wake"):
        resolve(Config.model_validate(d))


def test_toggle_on_button_needs_a_button():
    d = yaml.safe_load((EX / "plug-relay.yaml").read_text())
    del d["button"]
    with pytest.raises(ConfigError, match="toggle_on_button needs button"):
        resolve(Config.model_validate(d))


def test_network_left_resets_the_ias_enrollment(tmp_path):
    render(load(str(EX / "door-sensor.yaml")), tmp_path, EX)
    c = (tmp_path / "zb_device.c").read_text()
    assert "void zbr_network_left(void)" in c and "zbr_ias_reset(&ep1_ias)" in c
    assert "void zb_user_network_left(void);" in (tmp_path / "zb_user.h").read_text()


def test_relative_output_directory_is_made_absolute(tmp_path, monkeypatch):
    """west runs in the Zephyr tree, so a relative -o would point into it."""
    from zigbee_builder import cli
    seen = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("zigbee_builder.build.build", lambda project, out, env: seen.update(project=project, out=out))
    cli.main(["build", str(EX / "plug-relay.yaml"), "-o", "rel/out"])
    assert seen["project"].is_absolute() and seen["out"].is_absolute()


def test_search_on_power_on_can_be_turned_off(tmp_path):
    d = wall()
    d["service"]["search_on_power_on"] = False
    render(Config.model_validate(d), tmp_path, EX)
    g = (tmp_path / "zb_gen.h").read_text()
    assert "ZBR_SEARCH_ON_POWER_ON" not in g and "ZBR_SEARCH_ON_BOOT" not in g
    # no service input: it searches from boot anyway, the power-on rule is not needed
    del d["service"]
    render(Config.model_validate(d), tmp_path, EX)
    g = (tmp_path / "zb_gen.h").read_text()
    assert "ZBR_SEARCH_ON_BOOT" in g and "ZBR_SEARCH_ON_POWER_ON" not in g


def meter_overlay(tmp_path, **extra):
    d = yaml.safe_load((EX / "plug-bl0937.yaml").read_text())
    comp = next(c for c in d["components"] if c["type"] == "bl0937")
    for k in ("voltage_v_per_hz", "current_a_per_hz", "power_w_per_hz"):
        comp.pop(k, None)
    comp.update(extra)
    render(Config.model_validate(d), tmp_path, EX)
    return (tmp_path / "tlsr8258_generic.overlay").read_text()


def test_meter_scales_become_fractions(tmp_path):
    o = meter_overlay(tmp_path, voltage_v_per_hz="110/861", current_a_per_hz=0.012375,
                      power_w_per_hz="436/285")
    assert "voltage-mv-per-mhz = <110 861>;" in o
    assert "current-ma-per-mhz = <99 8000>;" in o
    assert "power-mw-per-mhz = <436 285>;" in o


def test_meter_without_scales_keeps_the_resistors(tmp_path):
    o = meter_overlay(tmp_path)
    assert "per-mhz" not in o and "voltage-divider-milli" in o


@pytest.mark.parametrize("bad", ["abc", 0, -1, "1/0"])
def test_meter_scale_must_be_a_positive_number(tmp_path, bad):
    with pytest.raises(ConfigError, match="voltage_v_per_hz"):
        meter_overlay(tmp_path, voltage_v_per_hz=bad)


def board(chip, name="plug-relay"):
    return load(str(EX / f"{name}.yaml"), str(EX / "boards" / chip / f"{name}.yaml"))


def test_esp32c6(tmp_path):
    cfg = board("esp32c6")
    assert cfg.zigbee.chip == "esp32c6"
    render(cfg, tmp_path, EX)
    ov = (tmp_path / "esp32c6_devkitc_esp32c6_hpcore.overlay").read_text()
    assert "esp32c6-pinctrl.h" in ov and "&gpio0 10" in ov and "DT_SIZE_M(4)" in ov
    assert "CONFIG_ZIGBEE_OTA=y" in (tmp_path / "prj.conf").read_text()
    d = cfg.model_dump()
    d["pins"]["relay"] = "GPIO24"                                      # an SPI flash pin
    with pytest.raises(ConfigError, match="reserved"):
        resolve(Config.model_validate(d))
    d["pins"]["relay"] = "GPIO30"
    with pytest.raises(ConfigError, match="does not exist on esp32c6"):
        resolve(Config.model_validate(d))


def test_bl702(tmp_path):
    render(board("bl702"), tmp_path, EX)
    conf = (tmp_path / "prj.conf").read_text()
    assert "CONFIG_ZIGBEE_OTA=y" in conf and 'CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION="0.1.0"' in conf
    ov = (tmp_path / "dt_xt_zb1_devkit.overlay").read_text()
    assert "&gpio0 3" in ov and "slot0_partition: partition@20000" in ov
    info = json.loads((tmp_path / "build_info.json").read_text())
    assert info["build"]["driver"] == "sysbuild" and info["ota"]
    assert info["flash"]["boot_header"] == "tool"


def test_bl702_i2c(tmp_path):
    d = yaml.safe_load((EX / "temp-humidity-sht30.yaml").read_text())
    d["zigbee"].update(chip="bl702", sleepy=False)
    d["components"] = [c for c in d["components"] if c["type"] != "battery_adc"]     # no battery ADC yet
    for e in d["endpoints"]:
        e["clusters"].pop("power_config", None)
    d["pins"] = {"sda": "GPIO1", "scl": "GPIO0", "button": {"pin": "GPIO10", "pull": "up", "inverted": True},
                 "led": "GPIO11"}
    render(Config.model_validate(d), tmp_path, EX)
    ov = (tmp_path / "dt_xt_zb1_devkit.overlay").read_text()
    assert "GPIO0_I2C_SCL" in ov and "GPIO1_I2C_SDA" in ov
    d["pins"]["sda"], d["pins"]["scl"] = "GPIO3", "GPIO4"             # not a hardware pair: bit-banged
    render(Config.model_validate(d), tmp_path, EX)
    assert "gpio-i2c" in (tmp_path / "dt_xt_zb1_devkit.overlay").read_text()


def test_phy6252(tmp_path):
    render(board("phy6252"), tmp_path, EX)
    ov = (tmp_path / "pb_03f.overlay").read_text()
    assert "&gpio0 2 " in ov and "&gpio0 7 " in ov                    # P2 is index 2, P11 is index 7
    info = json.loads((tmp_path / "build_info.json").read_text())
    assert info["build"]["driver"] == "sysbuild" and info["build"]["loader_image"] == "zephyr.phy6252.bin"
    assert "SB_CONFIG_MCUBOOT_MODE_SINGLE_APP=y" in info["build"]["sb_config"]
    assert info["toolchain"] == "gnuarmemb" and not info["ota"]        # one slot: no OTA file
    assert "slot0_partition: partition@10000" in ov and "DT_SIZE" not in ov


def test_phy6252_pins():
    d = board("phy6252").model_dump()
    d["pins"]["relay"] = "P9"                                          # the console
    with pytest.raises(ConfigError, match="reserved"):
        resolve(Config.model_validate(d))
    d["pins"]["relay"] = "P4"                                          # not a pad of the chip
    with pytest.raises(ConfigError, match="does not exist on phy6252"):
        resolve(Config.model_validate(d))


@pytest.mark.parametrize("chip", ["esp32c6", "bl702", "phy6252"])
def test_new_chips_stay_awake(chip):
    d = board(chip).model_dump()
    d["zigbee"].update(role="end_device", sleepy=True)
    with pytest.raises(ConfigError, match=f"not supported on {chip}"):
        resolve(Config.model_validate(d))


def meter_cfg(**clusters):
    d = yaml.safe_load((EX / "plug-bl0937.yaml").read_text())
    for e in d["endpoints"]:
        for k, v in clusters.items():
            if k in e["clusters"]:
                e["clusters"][k] = v
    return d


def test_pair_default_is_the_catalog_one(tmp_path):
    render(Config.model_validate(meter_cfg()), tmp_path, EX)
    c = (tmp_path / "zb_device.c").read_text()
    assert "ZCL_ATTRID_AC_VOLTAGE_DIVISOR" in c and "ZCL_ATTRID_DIVISOR" in c


def test_pair_override_on_the_binding(tmp_path):
    d = meter_cfg(electrical_measurement={"source": "meter",
                                          "rms_current": {"source": "meter", "multiplier": 1, "divisor": 100}})
    r = resolve(Config.model_validate(d))
    cur = next(a for ep in r["eps"] for cl in ep["clusters"] if cl["name"] == "electrical_measurement"
               for a in cl["attrs"] if a["name"] == "rms_current")
    div = next(a for ep in r["eps"] for cl in ep["clusters"] if cl["name"] == "electrical_measurement"
               for a in cl["attrs"] if a["name"] == "current_divisor")
    assert div["const"] == 100 and cur["a"]["scale"] == 0.01
    other = next(a for ep in r["eps"] for cl in ep["clusters"] if cl["name"] == "electrical_measurement"
                 for a in cl["attrs"] if a["name"] == "voltage_divisor")
    assert other["const"] == 100                                    # the rest keep the catalog's


def test_pair_is_not_bound_on_its_own():
    d = meter_cfg(electrical_measurement={"source": "meter", "current_divisor": "meter.current"})
    with pytest.raises(ConfigError, match="derived from"):
        resolve(Config.model_validate(d))


@pytest.mark.parametrize("bad,msg", [({"divisor": 0}, "positive integer"), ({"multiplier": 1.5}, "positive integer"),
                                     ({"divisor": 70000}, "does not fit"), ({"offset": 1}, "unknown option"),
                                     ({"multiplier": 1, "divisor": 3000}, "finer than the channel")])
def test_pair_checks(bad, msg):
    d = meter_cfg(electrical_measurement={"rms_current": {"source": "meter", **bad}})
    with pytest.raises(ConfigError, match=msg):
        resolve(Config.model_validate(d))


def test_pair_absent_without_the_value():
    d = meter_cfg(electrical_measurement={"rms_voltage": "meter.voltage"})    # only the voltage is bound
    r = resolve(Config.model_validate(d))
    names = [a["name"] for ep in r["eps"] for cl in ep["clusters"] if cl["name"] == "electrical_measurement"
             for a in cl["attrs"]]
    assert "voltage_divisor" in names and "current_divisor" not in names and "power_divisor" not in names


def test_concentration_clusters(tmp_path):
    d = yaml.safe_load((EX / "abstract-sensor.yaml").read_text())
    d["endpoints"][0]["clusters"] = {"co2": {}, "pm25": {}, "formaldehyde": {}}
    render(Config.model_validate(d), tmp_path, EX)
    c = (tmp_path / "zb_device.c").read_text()
    for fn in ("zbr_register_co2", "zbr_register_pm25", "zbr_register_formaldehyde"):
        assert fn in c
    assert (tmp_path / "runtime/zbr.c").read_text().count("ZBR_CONCENTRATION(") >= 3


def tuya():
    return yaml.safe_load((EX / "tuya-co2-r02.yaml").read_text())


def test_tuya_mcu_example(tmp_path):
    render(Config.model_validate(tuya()), tmp_path, EX)
    c = (tmp_path / "zb_device.c").read_text()
    # co2: ppm -> milli-ppm -> a float fraction; temperature 0.1 degC per count -> milli-degC -> 0.01 degC
    assert "zbr_dp_scale((int32_t)iotdrv_tuya_dp_u32(dp), 1000, 1)" in c
    assert "zbr_dp_scale((int32_t)iotdrv_tuya_dp_u32(dp), 100, 1)" in c
    assert "zbr_float_bits(air_co2, 1000000000U)" in c
    assert "if (ok_air && air_co2_valid)" in c                  # nothing is reported before the MCU said it
    conf = (tmp_path / "prj.conf").read_text()
    assert "CONFIG_IOTDRV_TUYA_MCU=y" in conf and "CONFIG_SERIAL=y" in conf and "CONFIG_SERIAL=n" not in conf
    ov = (tmp_path / "tlsr8258_generic.overlay").read_text()
    assert "current-speed = <9600>" in ov and "TLSR8258_GPIO_PB7" in ov


def test_tuya_mcu_fraction_per_count(tmp_path):
    d = tuya()
    d["components"][0]["channels"]["co2"]["per_count"] = "1/3"
    render(Config.model_validate(d), tmp_path, EX)
    assert "zbr_dp_scale((int32_t)iotdrv_tuya_dp_u32(dp), 1000, 3)" in (tmp_path / "zb_device.c").read_text()


@pytest.mark.parametrize("patch,msg", [
    (lambda d: d["components"][0].pop("uart"), "uart:"),
    (lambda d: d["pins"].update(mcu_tx="PB0"), "cannot be the UART TX"),
    (lambda d: d["pins"].update(mcu_rx="PB2"), "cannot be the UART RX"),
    (lambda d: d["components"][0]["channels"]["humidity"].update(dp=2), "is on"),
    (lambda d: d["components"][0]["channels"]["humidity"].update(dp=300), "not 0..255"),
    (lambda d: d["components"][0]["channels"]["humidity"].update(type="string"), "none of"),
    (lambda d: d["components"][0]["channels"]["humidity"].update(per_count=0), "positive"),
    (lambda d: d["components"][0]["channels"]["humidity"].pop("dp"), "needs dp"),
    (lambda d: d["components"][0].update(channels={}), "empty"),
])
def test_tuya_mcu_errors(patch, msg):
    d = tuya()
    patch(d)
    with pytest.raises(ConfigError, match=msg):
        resolve(Config.model_validate(d))


def test_tuya_mcu_needs_a_chip_with_a_uart():
    d = tuya()
    d["zigbee"]["chip"] = "esp32h2"
    d["pins"] = {"mcu_tx": "GPIO10", "mcu_rx": "GPIO11"}
    with pytest.raises(ConfigError, match="no UART description"):
        resolve(Config.model_validate(d))

# USB shell on nice!nano

This small Zephyr app checks USB CDC ACM and the shell on the `promicro_nrf52840/nrf52840/uf2` target. It registers
the `hello` command, which prints uptime.

Build it from the Zephyr workspace:

```sh
west build -p always -b promicro_nrf52840/nrf52840/uf2 \
  -d /tmp/nicenano-usb-shell /path/to/zigbee-devices-builder/examples/nrf52840-usb-shell
```

The output is `/tmp/nicenano-usb-shell/zephyr/zephyr.uf2`. Double-tap reset to enter the board's UF2 bootloader,
copy the file to its mounted drive, then open the new USB serial port at any baud rate and run `hello`.

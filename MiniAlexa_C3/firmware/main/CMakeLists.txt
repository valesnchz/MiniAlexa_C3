idf_component_register(SRCS "main.c" INCLUDE_DIRS "."
    REQUIRES esp_adc esp_driver_usb_serial_jtag esp_driver_gpio esp_driver_i2s esp_timer)

// Optional touchscreen: a tap anywhere advances to the next meme.
//
// On the ESP32-2432S028R the XPT2046 controller sits on its OWN SPI pins rather than sharing the
// display bus, so TFT_eSPI's built-in TOUCH_CS support does not work here -- hence a separate
// SPIClass and the XPT2046_Touchscreen library.
//
// Everything else works without this: if the wiring differs on your board revision, taps simply
// do nothing and no other feature is affected.
#pragma once

#include <Arduino.h>

namespace touch {

void begin();

// True once per tap (debounced, edge-triggered). Call every loop.
bool tapped();

}  // namespace touch

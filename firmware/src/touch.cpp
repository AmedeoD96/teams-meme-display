#include "touch.h"

#include <SPI.h>
#include <XPT2046_Touchscreen.h>

namespace touch {
namespace {

// CYD touch wiring. These are NOT the display's SPI pins.
constexpr uint8_t kTouchClk = 25;
constexpr uint8_t kTouchMiso = 39;
constexpr uint8_t kTouchMosi = 32;
constexpr uint8_t kTouchCs = 33;
constexpr uint8_t kTouchIrq = 36;

constexpr uint32_t kDebounceMs = 400;

SPIClass gTouchSpi(HSPI);
XPT2046_Touchscreen gTouch(kTouchCs, kTouchIrq);

bool gWasDown = false;
uint32_t gLastTapMs = 0;

}  // namespace

void begin() {
  gTouchSpi.begin(kTouchClk, kTouchMiso, kTouchMosi, kTouchCs);
  gTouch.begin(gTouchSpi);
  gTouch.setRotation(1);
}

bool tapped() {
  const bool down = gTouch.touched();
  const bool edge = down && !gWasDown;
  gWasDown = down;

  if (!edge) return false;
  const uint32_t now = millis();
  if (now - gLastTapMs < kDebounceMs) return false;
  gLastTapMs = now;
  return true;
}

}  // namespace touch

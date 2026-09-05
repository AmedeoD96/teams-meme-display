// Line-oriented serial protocol. See docs/PROTOCOL.md for the full command list.
#pragma once

#include <Arduino.h>

#include "status.h"

namespace serial_link {

struct Handlers {
  void (*onStatus)(Status status) = nullptr;
  void (*onNext)() = nullptr;
  void (*onBrightness)(uint8_t percent) = nullptr;
  void (*onRotate)(uint16_t seconds) = nullptr;
  void (*onTime)(const String &hhmm) = nullptr;
  void (*onLanguage)(Language language) = nullptr;
  void (*onOrientation)(Orientation orientation) = nullptr;
  void (*onDisplayMode)(DisplayMode mode) = nullptr;
  void (*onTransition)(uint16_t ms) = nullptr;
  void (*onTone)(Tone tone) = nullptr;
  //: The phrase to show. The PC owns the wording (see pc_app/phrases.py); an empty value hands
  //: the board back to its own flashed caption bank.
  void (*onCaption)(const String &caption) = nullptr;
};

void begin(const Handlers &handlers);

// Consume any complete lines waiting on the port. Call every loop.
void poll();

// millis() of the last recognised command, for the PC-timeout watchdog.
uint32_t lastCommandMs();

}  // namespace serial_link

// Where the board's replies go.
//
// Everything the firmware says -- READY:, LOG:, EVT:, PONG -- is written here rather than
// straight to Serial, because there are now two ways to be talking to the PC. Output is mirrored
// to USB whatever else is attached, so `pio device monitor` stays a complete view of the board
// even while the tray app is driving it over WiFi. See docs/PROTOCOL.md.
//
// Named say:: rather than the obvious link:: because POSIX declares a link()
// function, which unistd.h drags in behind the WiFi headers and which makes the
// namespace unusable in net_link.cpp.
#pragma once

#include <Arduino.h>

namespace say {

//: Longest line the board writes. Comfortably over the worst case, which is
//: "LOG:ignoring '<command>'" with a command as long as serial_link::kMaxLine.
constexpr size_t kMaxOutLine = 256;

//: Where a second copy of every line goes, or nullptr when nothing else is attached. Registered
//: by net_link so this layer needs to know nothing about WiFi.
void setSecondary(Stream *(*provider)());

void printf(const char *format, ...);
void println(const char *text);
void println(const __FlashStringHelper *text);

}  // namespace say

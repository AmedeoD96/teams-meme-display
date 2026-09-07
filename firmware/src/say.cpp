#include "say.h"

#include <stdarg.h>

namespace say {
namespace {

Stream *(*gSecondary)() = nullptr;

Stream *secondary() { return gSecondary == nullptr ? nullptr : gSecondary(); }

}  // namespace

void setSecondary(Stream *(*provider)()) { gSecondary = provider; }

// A note on blocking: WiFiClient::write() gives up after ten one-second selects, so a peer
// that stops reading without closing could stall loop() for that long. It needs a client
// whose receive window has filled, and the board only ever writes short lines at events the
// PC itself asked for -- so the case is a laptop that slept mid-connection, once, and the
// alternative (queueing output) would cost more than the problem is worth.

void printf(const char *format, ...) {
  char buffer[kMaxOutLine];
  va_list args;
  va_start(args, format);
  vsnprintf(buffer, sizeof(buffer), format, args);
  va_end(args);
  Serial.print(buffer);
  Stream *other = secondary();
  if (other != nullptr) other->print(buffer);
}

void println(const char *text) {
  Serial.println(text);
  Stream *other = secondary();
  if (other != nullptr) other->println(text);
}

void println(const __FlashStringHelper *text) {
  Serial.println(text);
  Stream *other = secondary();
  // print() pulls the string out of flash for us, so no copy is needed here.
  if (other != nullptr) other->println(text);
}

}  // namespace say

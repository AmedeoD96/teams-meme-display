#include "serial_link.h"

namespace serial_link {
namespace {

Handlers gHandlers;
String gBuffer;
uint32_t gLastCommandMs = 0;
bool gDiscarding = false;

// Guards against a peer that never sends a newline filling up RAM.
constexpr size_t kMaxLine = 96;

void dispatch(String line) {
  line.trim();
  if (line.isEmpty()) return;

  if (line == "PING") {
    Serial.println(F("PONG"));
    gLastCommandMs = millis();
    return;
  }

  const int colon = line.indexOf(':');
  const String command = (colon < 0) ? line : line.substring(0, colon);
  const String value = (colon < 0) ? String() : line.substring(colon + 1);

  if (command == "STATUS") {
    Status status;
    if (!statusFromToken(value, &status)) {
      Serial.printf("LOG:unknown status token '%s'\n", value.c_str());
      return;
    }
    // Only a recognised STATUS feeds the watchdog: that is the PC's heartbeat, and treating any
    // stray byte as liveness would hide a half-dead sender.
    gLastCommandMs = millis();
    if (gHandlers.onStatus) gHandlers.onStatus(status);
  } else if (command == "NEXT") {
    gLastCommandMs = millis();
    if (gHandlers.onNext) gHandlers.onNext();
  } else if (command == "BRIGHT") {
    gLastCommandMs = millis();
    if (gHandlers.onBrightness) gHandlers.onBrightness(constrain(value.toInt(), 0, 100));
  } else if (command == "ROTATE") {
    gLastCommandMs = millis();
    if (gHandlers.onRotate) gHandlers.onRotate(constrain(value.toInt(), 0, 3600));
  } else if (command == "TIME") {
    gLastCommandMs = millis();
    if (gHandlers.onTime) gHandlers.onTime(value);
  } else if (command == "LANG") {
    Language language;
    if (!languageFromToken(value, &language)) {
      Serial.printf("LOG:unknown language '%s'\n", value.c_str());
      return;
    }
    gLastCommandMs = millis();
    if (gHandlers.onLanguage) gHandlers.onLanguage(language);
  } else if (command == "ORIENT") {
    Orientation orientation;
    if (!orientationFromToken(value, &orientation)) {
      Serial.printf("LOG:unknown orientation '%s'\n", value.c_str());
      return;
    }
    gLastCommandMs = millis();
    if (gHandlers.onOrientation) gHandlers.onOrientation(orientation);
  } else if (command == "MODE") {
    DisplayMode displayMode;
    if (!displayModeFromToken(value, &displayMode)) {
      Serial.printf("LOG:unknown mode '%s'\n", value.c_str());
      return;
    }
    gLastCommandMs = millis();
    if (gHandlers.onDisplayMode) gHandlers.onDisplayMode(displayMode);
  } else if (command == "TRANSITION") {
    gLastCommandMs = millis();
    if (gHandlers.onTransition) gHandlers.onTransition(constrain(value.toInt(), 0, 2000));
  } else {
    Serial.printf("LOG:ignoring '%s'\n", command.c_str());
  }
}

}  // namespace

void begin(const Handlers &handlers) {
  gHandlers = handlers;
  gBuffer.reserve(kMaxLine);
  gLastCommandMs = millis();
}

void poll() {
  while (Serial.available()) {
    const char c = static_cast<char>(Serial.read());
    if (c == '\n') {
      if (!gDiscarding) dispatch(gBuffer);
      gBuffer = "";
      gDiscarding = false;
    } else if (c != '\r' && !gDiscarding) {
      if (gBuffer.length() < kMaxLine) {
        gBuffer += c;
      } else {
        // Overlong line: discard the rest of it rather than truncating into something that
        // happens to parse as a valid command.
        gBuffer = "";
        gDiscarding = true;
      }
    }
  }
}

uint32_t lastCommandMs() { return gLastCommandMs; }

}  // namespace serial_link

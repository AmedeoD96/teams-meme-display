#include "serial_link.h"

namespace serial_link {
namespace {

Handlers gHandlers;
String gBuffer;
uint32_t gLastCommandMs = 0;
uint32_t gLastByteMs = 0;
bool gDiscarding = false;

// How long a half-finished line is allowed to sit before it is treated as noise. Longer than any
// gap inside a line the PC sends -- even a GIFDATA chunk arrives in one burst at 115200.
constexpr uint32_t kPartialLineMs = 250;

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
  } else if (command == "TONE") {
    Tone tone;
    if (!toneFromToken(value, &tone)) {
      Serial.printf("LOG:unknown tone '%s'\n", value.c_str());
      return;
    }
    gLastCommandMs = millis();
    if (gHandlers.onTone) gHandlers.onTone(tone);
  } else if (command == "CAPTION") {
    gLastCommandMs = millis();
    if (gHandlers.onCaption) gHandlers.onCaption(value);
  } else if (command == "ALERT") {
    gLastCommandMs = millis();
    // Clamped rather than rejected: a bad value should still show *something* briefly instead of
    // parking the GIF on screen forever.
    if (gHandlers.onAlert) gHandlers.onAlert(constrain(value.toInt(), 200, 60000));
  } else if (command == "GIFBEGIN") {
    gLastCommandMs = millis();
    // "<bytes>:<crc32>". Both unsigned 32-bit, so they are parsed as 64-bit and narrowed --
    // toInt() is signed and a CRC above 2^31 would come back negative.
    const int split = value.indexOf(':');
    if (split < 0) {
      Serial.println(F("EVT:GIFERR:malformed GIFBEGIN"));
      return;
    }
    if (gHandlers.onGifBegin) {
      gHandlers.onGifBegin(strtoul(value.substring(0, split).c_str(), nullptr, 10),
                           strtoul(value.substring(split + 1).c_str(), nullptr, 10));
    }
  } else if (command == "GIFDATA") {
    gLastCommandMs = millis();
    if (gHandlers.onGifData) gHandlers.onGifData(value);
  } else if (command == "GIFEND") {
    gLastCommandMs = millis();
    if (gHandlers.onGifEnd) gHandlers.onGifEnd();
  } else {
    Serial.printf("LOG:ignoring '%s'\n", command.c_str());
  }
}

}  // namespace

void begin(const Handlers &handlers) {
  gHandlers = handlers;
  gBuffer.reserve(kMaxLine);
  gLastCommandMs = millis();
  gLastByteMs = millis();
}

void poll() {
  // A partial line that stopped arriving is junk: a burst of noise from the USB bridge as the PC
  // boots or opens the port, or a sender that died mid-line. Dropping it matters because the next
  // real command would otherwise be glued onto it and parse as nothing -- which is how a board
  // plugged in at boot could stay unreachable until it was power-cycled.
  // Only with nothing waiting to be read: rendering a frame can hold up the loop for longer than
  // this timeout, and the rest of a perfectly good line would be sitting in the FIFO meanwhile.
  if (!Serial.available() && (gBuffer.length() > 0 || gDiscarding) &&
      millis() - gLastByteMs > kPartialLineMs) {
    gBuffer = "";
    gDiscarding = false;
  }
  while (Serial.available()) {
    gLastByteMs = millis();
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

// Teams status meme display -- ESP32-2432S028R firmware.
//
// The PC tray app pushes the current Teams status over USB serial; this shows a matching meme
// with a rotating caption. See docs/PROTOCOL.md for the wire format.

#include <Arduino.h>
#include <Preferences.h>
#include <esp_random.h>

#include "content.h"
#include "display.h"
#include "serial_link.h"
#include "status.h"
#include "touch.h"

namespace {

constexpr char kVersion[] = "1.2.0";

// Show DISCONNECTED if the PC stops sending its STATUS heartbeat for this long. The app resends
// every 5s by default, so this tolerates two missed beats before complaining.
constexpr uint32_t kPcTimeoutMs = 15000;

Preferences gPrefs;

Status gStatus = Status::Disconnected;
uint16_t gRotateSeconds = 30;
uint32_t gLastFrameMs = 0;
bool gTimedOut = true;

void renderFrame() {
  // Text mode never draws a meme, so do not spend a LittleFS read picking one -- this runs on
  // every rotation tick.
  const String meme =
      display::mode() == DisplayMode::Text ? String() : content::nextMeme(gStatus);
  const String caption = content::nextCaption(gStatus);
  display::showFrame(gStatus, content::language(), meme, caption);
  gLastFrameMs = millis();
}

void applyStatus(Status status) {
  if (gTimedOut) {
    Serial.println(F("LOG:PC is back"));
    gTimedOut = false;
  }
  if (status == gStatus) return;
  gStatus = status;
  Serial.printf("LOG:status %s\n", statusToken(status));
  renderFrame();
}

void onStatus(Status status) { applyStatus(status); }

void onNext() { renderFrame(); }

// The PC re-sends its settings on every reconnect, so only touch NVS when a value actually
// changed -- otherwise a board that reconnects in a loop would keep writing flash.
void onBrightness(uint8_t percent) {
  if (percent == display::brightness()) return;
  display::setBrightness(percent);
  gPrefs.putUChar("bright", percent);
}

void onRotate(uint16_t seconds) {
  if (seconds == gRotateSeconds) return;
  gRotateSeconds = seconds;
  gPrefs.putUShort("rotate", seconds);
}

void onTime(const String &hhmm) { display::showClock(hhmm); }

void onDisplayMode(DisplayMode displayMode) {
  if (displayMode == display::mode()) return;
  display::setMode(displayMode);
  gPrefs.putUChar("mode", static_cast<uint8_t>(displayMode));
  renderFrame();
}

void onTransition(uint16_t ms) {
  if (ms == display::transitionMs()) return;
  display::setTransitionMs(ms);
  gPrefs.putUShort("trans", ms);
}

void onLanguage(Language newLanguage) {
  if (newLanguage == content::language()) return;
  content::setLanguage(newLanguage);
  gPrefs.putUChar("lang", static_cast<uint8_t>(newLanguage));
  renderFrame();  // the caption on screen is in the old language
}

void onOrientation(Orientation newOrientation) {
  if (newOrientation == display::orientation()) return;
  display::setOrientation(newOrientation);
  content::setOrientation(newOrientation);  // each orientation has its own meme folder
  gPrefs.putUChar("orient", static_cast<uint8_t>(newOrientation));
  renderFrame();
}

// Drop to DISCONNECTED when the PC goes quiet, so the screen never shows a status that stopped
// being true when the tray app was closed or the cable pulled.
void checkPcTimeout() {
  if (gTimedOut) return;
  if (millis() - serial_link::lastCommandMs() < kPcTimeoutMs) return;
  gTimedOut = true;
  Serial.println(F("LOG:PC timeout"));
  gStatus = Status::Disconnected;
  renderFrame();
}

void checkRotation() {
  if (gRotateSeconds == 0) return;
  if (millis() - gLastFrameMs < static_cast<uint32_t>(gRotateSeconds) * 1000UL) return;
  // Nothing to rotate through if there is at most one meme for this status; the caption still
  // changes, which is reason enough to repaint.
  renderFrame();
}

// Read an enum back from NVS, clamping anything out of range to the default.
template <typename E>
E storedEnum(const char *key, uint8_t count, E fallback) {
  const uint8_t raw = gPrefs.getUChar(key, static_cast<uint8_t>(fallback));
  return raw < count ? static_cast<E>(raw) : fallback;
}

}  // namespace

void setup() {
  Serial.begin(115200);

  gPrefs.begin("teamsmeme", false);
  const uint8_t brightness = gPrefs.getUChar("bright", 80);
  gRotateSeconds = gPrefs.getUShort("rotate", 30);
  // Defaults match pc_app/config.py, so a board with no PC attached looks the same as one
  // driven by a fresh install.
  const Language language = storedEnum("lang", kLanguageCount, Language::It);
  const Orientation orientation = storedEnum("orient", kOrientationCount, Orientation::Portrait);
  const DisplayMode displayMode = storedEnum("mode", kDisplayModeCount, DisplayMode::Image);

  display::begin(orientation, displayMode);
  display::setBrightness(brightness);
  display::setTransitionMs(gPrefs.getUShort("trans", 400));
  touch::begin();
  content::begin(orientation, language);

  // The ESP32's hardware RNG, so the meme order differs between boots.
  randomSeed(esp_random());

  serial_link::Handlers handlers;
  handlers.onStatus = onStatus;
  handlers.onNext = onNext;
  handlers.onBrightness = onBrightness;
  handlers.onRotate = onRotate;
  handlers.onTime = onTime;
  handlers.onLanguage = onLanguage;
  handlers.onOrientation = onOrientation;
  handlers.onDisplayMode = onDisplayMode;
  handlers.onTransition = onTransition;
  serial_link::begin(handlers);

  Serial.printf("LOG:%u memes (%s), language %s, mode %s\n", content::totalMemes(),
                orientationFolder(orientation), languageCode(language),
                displayModeName(displayMode));
  renderFrame();
  Serial.printf("READY:%s\n", kVersion);
}

void loop() {
  serial_link::poll();
  if (touch::tapped()) renderFrame();
  checkPcTimeout();
  checkRotation();
  delay(10);
}

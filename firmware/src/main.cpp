// Teams status meme display -- ESP32-2432S028R firmware.
//
// The PC tray app pushes the current Teams status over USB serial; this shows a matching meme
// with a rotating caption. See docs/PROTOCOL.md for the wire format.

#include <Arduino.h>
#include <Preferences.h>
#include <esp_random.h>

#include "alert.h"
#include "content.h"
#include "display.h"
#include "net_link.h"
#include "say.h"
#include "serial_link.h"
#include "status.h"
#include "touch.h"

namespace {

constexpr char kVersion[] = "1.4.0";

// Show DISCONNECTED if the PC stops sending its STATUS heartbeat for this long. The app resends
// every 5s by default, so this tolerates two missed beats before complaining.
constexpr uint32_t kPcTimeoutMs = 15000;

Preferences gPrefs;

Status gStatus = Status::Disconnected;
Tone gTone = Tone::Normal;
uint16_t gRotateSeconds = 30;
uint32_t gLastFrameMs = 0;
bool gTimedOut = true;

// The phrase the PC last sent. While this is set the PC owns the wording -- it holds the phrase
// bank and knows the tone -- and the flashed bank in LittleFS is only the fallback for when
// nothing is driving us. Cleared on a PC timeout, which is what hands the board back to itself.
String gPcCaption;

// What net_link wants said while nothing is driving us -- the board's own address, once WiFi
// is up. It is the only place that address exists until somebody connects, and the board may
// well be on a battery on the other side of the desk.
String gNetCaption;

void renderFrame() {
  // The alert owns the panel while it is up. Whatever changed underneath is drawn when it ends,
  // from a clean slate -- see the alert::tick() branch in loop().
  if (alert::active()) return;
  // Only image mode draws a meme, so do not spend a LittleFS read picking one otherwise -- this
  // runs on every rotation tick.
  const String meme =
      display::mode() == DisplayMode::Image ? content::nextMeme(gStatus) : String();
  // The PC's words win; the network note stands in for while there is no PC at all.
  String caption = gPcCaption;
  if (caption.isEmpty() && gStatus == Status::Disconnected) caption = gNetCaption;
  if (caption.isEmpty()) caption = content::nextCaption(gStatus);
  display::showFrame(gStatus, content::language(), gTone, meme, caption);
  gLastFrameMs = millis();
}

void applyStatus(Status status) {
  if (gTimedOut) {
    say::println(F("LOG:PC is back"));
    gTimedOut = false;
  }
  if (status == gStatus) return;
  gStatus = status;
  say::printf("LOG:status %s\n", statusToken(status));
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

void onTone(Tone tone) {
  if (tone == gTone) return;
  gTone = tone;
  gPrefs.putUChar("tone", static_cast<uint8_t>(tone));
  say::printf("LOG:tone %s\n", toneName(tone));
  renderFrame();  // the mascot is wearing the old expression
}

// The PC choosing our words. Repeats are ignored so a resend costs nothing.
void onCaption(const String &caption) {
  if (caption == gPcCaption) return;
  gPcCaption = caption;
  renderFrame();
}

void onAlert(uint32_t durationMs) {
  // A failed play() leaves the status display exactly as it was, so there is nothing to repaint.
  alert::play(durationMs);
}

// The upload is alert.cpp's business end to end -- it owns the file and the checksum -- so these
// only forward. The board answers each one on the wire; see docs/PROTOCOL.md.
void onGifBegin(uint32_t bytes, uint32_t crc) { alert::uploadBegin(bytes, crc); }

void onGifData(const String &encoded) { alert::uploadChunk(encoded); }

void onGifEnd() { alert::uploadEnd(); }

// WiFi provisioning. serial_link only accepts these over USB; net_link does the work.
void onWifiSsid(const String &ssid) { net_link::stageSsid(ssid); }

void onWifiPassword(const String &base64) { net_link::stagePassword(base64); }

void onWifiApply() { net_link::apply(); }

void onWifiOff() { net_link::disable(); }

void onWifiStatus() { net_link::reportStatus(); }

void onTokenGet() { net_link::reportToken(); }

// The board saying where it is, for while nobody has connected to it yet.
void onNetInfo(const String &text) {
  if (text == gNetCaption) return;
  gNetCaption = text;
  // Only worth a repaint while it is the thing on screen; otherwise the next frame has it.
  if (gPcCaption.isEmpty() && gStatus == Status::Disconnected) renderFrame();
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
  say::println(F("LOG:PC timeout"));
  gStatus = Status::Disconnected;
  // Nobody is choosing our words any more, so fall back to the flashed bank rather than leaving
  // the last thing the PC said frozen on screen.
  gPcCaption = "";
  renderFrame();
}

void checkRotation() {
  if (gRotateSeconds == 0 || alert::active()) return;
  if (millis() - gLastFrameMs < static_cast<uint32_t>(gRotateSeconds) * 1000UL) return;
  // With a PC attached the wording is its business, and it runs a rotation timer of its own.
  // Outside image mode there is nothing else to rotate, so repainting here would only fight it.
  if (!gPcCaption.isEmpty() && display::mode() != DisplayMode::Image) {
    gLastFrameMs = millis();
    return;
  }
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
  // Must precede begin(). The default 256 bytes is about 22ms of runway at 115200, which is less
  // than one GIF frame takes to decode -- and far less than a GIF upload needs to stay ahead of
  // the LittleFS writes. See docs/PROTOCOL.md.
  Serial.setRxBufferSize(4096);
  Serial.begin(115200);

  gPrefs.begin("teamsmeme", false);
  const uint8_t brightness = gPrefs.getUChar("bright", 80);
  gRotateSeconds = gPrefs.getUShort("rotate", 30);
  // Defaults match pc_app/config.py, so a board with no PC attached looks the same as one
  // driven by a fresh install.
  const Language language = storedEnum("lang", kLanguageCount, Language::It);
  const Orientation orientation = storedEnum("orient", kOrientationCount, Orientation::Portrait);
  const DisplayMode displayMode = storedEnum("mode", kDisplayModeCount, DisplayMode::Mascot);
  gTone = storedEnum("tone", kToneCount, Tone::Normal);

  display::begin(orientation, displayMode);
  display::setBrightness(brightness);
  display::setTransitionMs(gPrefs.getUShort("trans", 400));
  touch::begin();
  alert::begin(display::panel());
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
  handlers.onTone = onTone;
  handlers.onCaption = onCaption;
  handlers.onAlert = onAlert;
  handlers.onGifBegin = onGifBegin;
  handlers.onGifData = onGifData;
  handlers.onGifEnd = onGifEnd;
  handlers.onWifiSsid = onWifiSsid;
  handlers.onWifiPassword = onWifiPassword;
  handlers.onWifiApply = onWifiApply;
  handlers.onWifiOff = onWifiOff;
  handlers.onWifiStatus = onWifiStatus;
  handlers.onTokenGet = onTokenGet;
  serial_link::begin(handlers);

  // After the display, deliberately: the mascot sprite is tens of KB and WiFi wants ~50KB of
  // its own, and the sprite is the one whose fallback (mascot::degraded()) costs you
  // something to look at.
  net_link::Callbacks netCallbacks;
  netCallbacks.onInfo = onNetInfo;
  net_link::begin(kVersion, netCallbacks);

  say::printf("LOG:%u memes (%s), language %s, mode %s\n", content::totalMemes(),
               orientationFolder(orientation), languageCode(language),
               displayModeName(displayMode));
  renderFrame();
  say::printf("LOG:%u bytes of heap free\n", static_cast<unsigned>(ESP.getFreeHeap()));
  say::printf("READY:%s\n", kVersion);
}

void loop() {
  // Before poll(), so a client that just authenticated is already a channel.
  net_link::tick();
  serial_link::poll();
  if (touch::tapped()) {
    // A tap during an alert dismisses it rather than asking for a new meme -- the GIF is in the
    // way, and getting rid of it is the obvious thing a tap should do.
    if (alert::active()) {
      alert::stop();
      display::invalidate();
      renderFrame();
    } else {
      // Tell the PC so it can send a fresh phrase; the meme we can change on our own.
      say::println(F("EVT:NEXT"));
      renderFrame();
    }
  }
  checkPcTimeout();
  checkRotation();
  if (alert::tick()) {
    // The alert just ended and it painted over everything, so the status display is rebuilt from
    // scratch rather than caption-faded onto a screen that is no longer there.
    display::invalidate();
    renderFrame();
  }
  // Drives the mascot animation and its caption fade. Returns immediately in the other modes.
  if (!alert::active()) display::tick(gStatus, gTone);
  delay(10);
}

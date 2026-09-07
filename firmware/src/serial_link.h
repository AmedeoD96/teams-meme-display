// Line-oriented command protocol. See docs/PROTOCOL.md for the full command list.
//
// The same parser serves both transports: USB serial, and the TCP client net_link hands over once
// it has authenticated. Each has its own line buffer, because bytes from the two interleave.
#pragma once

#include <Arduino.h>

#include "status.h"

namespace serial_link {

//: Longest line accepted, prefix included. Anything longer is discarded rather than truncated
//: into something that happens to parse. Long enough for the longest CAPTION: the PC will send
//: (MAX_PHRASE_CHARS in pc_app/phrases.py) and for a GIFDATA: chunk (CHUNK_BYTES in
//: pc_app/gif_upload.py, which is sized against this number).
constexpr size_t kMaxLine = 160;

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
  //: Play the alert GIF for this many milliseconds. The PC decides when -- it is the side that
  //: knows the notifications and the working hours.
  void (*onAlert)(uint32_t durationMs) = nullptr;
  //: GIF upload, in three parts. See "Uploading a GIF" in docs/PROTOCOL.md.
  void (*onGifBegin)(uint32_t bytes, uint32_t crc) = nullptr;
  void (*onGifData)(const String &encoded) = nullptr;
  void (*onGifEnd)() = nullptr;
  //: WiFi provisioning. Accepted over USB only -- see kUsbOnly in serial_link.cpp -- so that
  //: nobody on the network can rewrite the credentials or read the token back.
  void (*onWifiSsid)(const String &ssid) = nullptr;
  void (*onWifiPassword)(const String &base64) = nullptr;
  void (*onWifiApply)() = nullptr;
  void (*onWifiOff)() = nullptr;
  void (*onWifiStatus)() = nullptr;
  void (*onTokenGet)() = nullptr;
};

void begin(const Handlers &handlers);

//: The second transport's stream, or nullptr when no client is attached. Registered by net_link
//: so this module needs to know nothing about WiFi.
void setSecondary(Stream *(*provider)());

// Consume any complete lines waiting on either transport. Call every loop.
void poll();

// millis() of the last recognised command, for the PC-timeout watchdog.
uint32_t lastCommandMs();

}  // namespace serial_link

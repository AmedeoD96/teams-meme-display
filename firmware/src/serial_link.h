// Line-oriented serial protocol. See docs/PROTOCOL.md for the full command list.
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
};

void begin(const Handlers &handlers);

// Consume any complete lines waiting on the port. Call every loop.
void poll();

// millis() of the last recognised command, for the PC-timeout watchdog.
uint32_t lastCommandMs();

}  // namespace serial_link

// Everything that puts pixels on the panel: meme JPEGs, the caption band, the text-only scene.
#pragma once

#include <Arduino.h>

#include "status.h"

namespace display {

// Caption band geometry for image mode. Mirrored by CAPTION_* in tools/build_memes.py so that
// --preview renders what the device will actually draw.
constexpr int16_t kCaptionPadX = 6;
constexpr int16_t kCaptionPadY = 6;
constexpr int16_t kCaptionLineH = 16;
constexpr uint8_t kCaptionMaxLines = 3;

//: Text mode has the whole screen, so a caption can breathe over more lines.
constexpr uint8_t kTextModeMaxLines = 8;

void begin(Orientation orientation, DisplayMode mode);

// Re-rotates the panel. The caller must repaint afterwards -- the screen is cleared.
void setOrientation(Orientation orientation);
Orientation orientation();

// Image (meme + caption band) or Text (caption alone on the status colour). The caller must
// repaint afterwards.
void setMode(DisplayMode mode);
DisplayMode mode();

// Total milliseconds a caption change is allowed to take. 0 draws instantly.
void setTransitionMs(uint16_t ms);
uint16_t transitionMs();

// Live panel size; these swap over when the orientation changes.
int16_t width();
int16_t height();

// 0-100. Persisted by the caller; this only drives the backlight.
void setBrightness(uint8_t percent);
uint8_t brightness();

// Draw a frame. When only the caption changed since the last call, the caption is cross-faded
// in place rather than the whole screen being repainted.
void showFrame(Status status, Language language, const String &memePath, const String &caption);

// Forget what is on screen, so the next showFrame() repaints everything. Call after anything
// that invalidates the panel (orientation or mode change).
void invalidate();

// The clock overlay is redrawn on its own so a TIME: line does not cost a full repaint.
void showClock(const String &hhmm);

}  // namespace display

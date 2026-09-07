// Everything that puts pixels on the panel: meme JPEGs, the caption band, the text-only scene.
#pragma once

#include <Arduino.h>
#include <TFT_eSPI.h>

#include "status.h"

namespace display {

// Caption band geometry for image mode. Mirrored by CAPTION_* in tools/build_memes.py so that
// --preview renders what the device will actually draw.
constexpr int16_t kCaptionPadX = 6;
constexpr int16_t kCaptionPadY = 6;

// The band picks the largest font the caption fits in. Font 4 is far easier to read from across
// a desk, but it is wide, so a long phrase drops to font 2 rather than being cut off. Mirrors
// the CAPTION_* constants in pc_app/render.py.
//
// The big font gets four lines rather than three. At three, a phrase needing a fourth line fell
// all the way to font 2 -- which then wanted only two lines, so the caption was drawn at 62% size
// in a band left half empty. Across both shipped banks and a 530-phrase edited one, four lines is
// enough for every phrase in either orientation, and the fallback stops being reachable in
// practice.
constexpr uint8_t kCaptionFontBig = 4;
constexpr int16_t kCaptionLineHBig = 26;
constexpr uint8_t kCaptionLinesBig = 4;
constexpr uint8_t kCaptionFontSmall = 2;
constexpr int16_t kCaptionLineHSmall = 16;
//: Six of these still fit inside the reserve below, so the fallback can use the whole band.
constexpr uint8_t kCaptionLinesSmall = 6;

//: Array sizing. It must exceed the big font's budget too: layoutCaptionBand() tells "fits" from
//: "would be cut short" by giving wrapText() more slots than the budget and seeing how many come
//: back, and that stops working the moment the array is only as large as the budget itself.
constexpr uint8_t kCaptionMaxLines =
    kCaptionLinesSmall > kCaptionLinesBig ? kCaptionLinesSmall : kCaptionLinesBig + 1;
//: The tallest the band can get, which is the space the mascot keeps clear of. The big font
//: at its line limit is taller than the small font at its own, so that is the bound.
constexpr int16_t kCaptionReserve = kCaptionLinesBig * kCaptionLineHBig + 2 * kCaptionPadY;
static_assert(kCaptionLinesSmall * kCaptionLineHSmall + 2 * kCaptionPadY <= kCaptionReserve,
              "the small font's tallest band must still fit the space the scene keeps clear");

//: Text mode has the whole screen, so a caption can breathe over more lines.
constexpr uint8_t kTextModeMaxLines = 8;

void begin(Orientation orientation, DisplayMode mode);

// The panel itself, for the one other module that draws straight at it: the alert GIF, which
// takes the whole screen over rather than composing into a frame. Valid only after begin().
TFT_eSPI *panel();

// Re-rotates the panel. The caller must repaint afterwards -- the screen is cleared.
void setOrientation(Orientation orientation);
Orientation orientation();

// Image (meme + caption band), Text (caption alone on the status colour) or Mascot (the animated
// character above a caption band). The caller must repaint afterwards.
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
void showFrame(Status status, Language language, Tone tone, const String &memePath,
               const String &caption);

// Advance the mascot animation and any caption fade in progress. Call every loop; it returns
// immediately unless a frame is actually due. Does nothing outside mascot mode.
//
// Unlike the fades in image and text mode, nothing here blocks: a delay() between fade steps
// would stall the animation for the length of the transition.
void tick(Status status, Tone tone);

// Forget what is on screen, so the next showFrame() repaints everything. Call after anything
// that invalidates the panel (orientation or mode change).
void invalidate();

// The clock overlay is redrawn on its own so a TIME: line does not cost a full repaint.
void showClock(const String &hhmm);

}  // namespace display

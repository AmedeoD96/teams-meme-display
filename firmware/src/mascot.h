// The animated mascot: an original Teams-flavoured character whose expression follows the status
// and the tone.
//
// Deliberately drawn from shapes rather than flashed as images. A 240x320 animation frame is
// ~150 KB as a bitmap and ~100 ms to decode as a JPEG, and this board has neither the flash nor
// the PSRAM for that -- but it has plenty of cycles, so the character is composed into a sprite
// every frame instead. That also makes an expression a handful of bytes (see mascot_table.h)
// rather than an asset, which is why all 27 status/tone combinations cost nothing.
//
// The character is inspired by the Teams palette and its rounded "T"; it is not the Microsoft
// Teams logo, which is a trademark and is not reproduced here.
#pragma once

#include <Arduino.h>
#include <TFT_eSPI.h>

#include "mascot_table.h"
#include "status.h"

namespace mascot {

//: Largest sprite we will allocate for the character. Mirrors MASCOT_MAX_SIZE in pc_app/render.py.
//: Kept well under the space available: the caption is what people actually read, so the
//: character gives way to it rather than the other way round.
constexpr int16_t kMaxSize = 130;
//: Space left around the character inside the caption-free area.
constexpr int16_t kMargin = 20;
//: How far the character may drift from its resting place. The sprite is grown by this on each
//: side so a bob or a sway is never clipped.
constexpr int16_t kMotion = 6;
//: ~25 fps. Pushing a 172x172 sprite takes about 8 ms at 55 MHz, so this leaves plenty of slack.
constexpr uint16_t kFrameMs = 40;

// Attach to the panel and allocate the sprite. Safe to call more than once.
void begin(TFT_eSPI *panel);

// Recompute where the character sits. Mirrors mascot_box() in pc_app/render.py.
void layout(int16_t width, int16_t height, int16_t captionReserve);

// The colour behind the character: the status colour, dimmed. The full colour would clash with
// the purple body and swallow the presence badge.
uint16_t backdrop(Status status);

// Whether kFrameMs has passed since the last frame.
bool due(uint32_t nowMs);

// Draw the character at the animation phase implied by *nowMs*.
void render(Status status, Tone tone, uint32_t nowMs);

// Forget the animation phase and the blink schedule, so the next render starts clean.
void reset();

// True when the sprite could not be allocated and we are drawing straight to the panel.
bool degraded();

}  // namespace mascot

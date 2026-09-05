#include "display.h"

#include <LittleFS.h>
#include <TFT_eSPI.h>
#include <TJpg_Decoder.h>

#include "mascot.h"

namespace display {
namespace {

TFT_eSPI tft;
uint8_t gBrightness = 80;
Orientation gOrientation = Orientation::Landscape;
DisplayMode gMode = DisplayMode::Image;
uint16_t gTransitionMs = 400;
String gClock;

// What is currently on screen, so showFrame() can tell a caption change from a full repaint.
bool gHaveFrame = false;
Status gLastStatus = Status::Unknown;
String gLastCaption;

// Backlight PWM. TFT_BL is driven by hand rather than left to TFT_eSPI so brightness can be
// changed at runtime by the BRIGHT: command.
constexpr uint8_t kBacklightChannel = 0;
constexpr uint32_t kBacklightFreq = 5000;
constexpr uint8_t kBacklightBits = 8;

constexpr uint16_t kCaptionBg = TFT_BLACK;
constexpr uint16_t kCaptionFg = TFT_WHITE;
constexpr uint8_t kTextModeFont = 4;  // font 4: 26px tall, for the roomier text-only layout
constexpr int16_t kTextModeLineH = 26;

// The status badge that heads the text-mode screen, in place of the status name.
constexpr int16_t kTextBadgeCy = 26;
constexpr int16_t kTextBadgeR = 16;
constexpr int16_t kTextRuleY = 50;
//: Where the caption block starts, clear of the badge and its rule.
constexpr int16_t kTextTop = 58;

// How the caption band is currently laid out. The font is chosen per caption, so the height is
// not fixed and the band has to remember where its top was -- see layoutCaptionBand().
struct BandLayout {
  String lines[kCaptionMaxLines];
  uint8_t count = 0;
  uint8_t font = kCaptionFontSmall;
  int16_t lineH = kCaptionLineHSmall;
  int16_t top = 0;
};

//: Top of the band as last painted, or -1 when nothing is there. A caption that wraps to fewer
//: lines than the one before it needs a shorter band, and clearing only the shorter band would
//: leave the tail of the old caption stranded above it.
int16_t gBandTop = -1;

//: Steps in a fade. Enough to look continuous without making a short transition feel steppy.
constexpr uint8_t kFadeSteps = 10;

// Mascot mode fades its caption from the animation tick instead of blocking in a delay() loop,
// so this holds the fade in progress between frames.
struct SteppedFade {
  bool active = false;
  uint8_t step = 0;
  uint32_t nextMs = 0;
  uint16_t perStepMs = 0;
  BandLayout band;
};

SteppedFade gCaptionFade;

// The panel is natively 240x320 portrait, so rotation 0 is portrait and rotation 1 landscape.
uint8_t rotationFor(Orientation orientation) {
  return orientation == Orientation::Portrait ? 0 : 1;
}

// -- colour helpers ---------------------------------------------------------------------

uint8_t r5(uint16_t c) { return (c >> 11) & 0x1F; }
uint8_t g6(uint16_t c) { return (c >> 5) & 0x3F; }
uint8_t b5(uint16_t c) { return c & 0x1F; }

uint16_t rgb565From(uint8_t r, uint8_t g, uint8_t b) {
  return static_cast<uint16_t>((r << 11) | (g << 5) | b);
}

// Blend two RGB565 colours; t runs 0..steps.
uint16_t lerpColour(uint16_t from, uint16_t to, uint8_t t, uint8_t steps) {
  if (steps == 0) return to;
  return rgb565From(r5(from) + (r5(to) - r5(from)) * t / steps,
                    g6(from) + (g6(to) - g6(from)) * t / steps,
                    b5(from) + (b5(to) - b5(from)) * t / steps);
}

// Pick black or white for legibility on *background*. The green and amber themes are light
// enough that white text on them is genuinely hard to read.
uint16_t contrastOn(uint16_t background) {
  // Rec. 601 luma, on the 0..255 scale each channel is expanded back to.
  const uint16_t luma = (r5(background) * 255 / 31) * 299 / 1000 +
                        (g6(background) * 255 / 63) * 587 / 1000 +
                        (b5(background) * 255 / 31) * 114 / 1000;
  return luma > 140 ? TFT_BLACK : TFT_WHITE;
}

// TJpg_Decoder hands us decoded blocks as it goes, which keeps peak RAM tiny -- there is no
// PSRAM on this board, so a full 320x240x2 = 150KB framebuffer is not an option.
bool pushJpegBlock(int16_t x, int16_t y, uint16_t w, uint16_t h, uint16_t *bitmap) {
  if (y >= tft.height()) return false;  // stop decoding once we are off-screen
  tft.pushImage(x, y, w, h, bitmap);
  return true;
}

// -- text layout ------------------------------------------------------------------------

// Greedy word wrap by measured pixel width. The fonts are proportional, so wrapping by
// character count would be wrong for anything with wide or narrow letters.
uint8_t wrapText(const String &text, String *lines, uint8_t maxLines, int16_t maxWidth,
                 uint8_t font) {
  uint8_t count = 0;
  String current;
  int start = 0;

  while (start <= text.length() && count < maxLines) {
    int space = text.indexOf(' ', start);
    String word = (space < 0) ? text.substring(start) : text.substring(start, space);
    start = (space < 0) ? text.length() + 1 : space + 1;
    if (word.isEmpty()) continue;

    String candidate = current.isEmpty() ? word : current + " " + word;
    if (tft.textWidth(candidate, font) <= maxWidth) {
      current = candidate;
      continue;
    }
    if (!current.isEmpty()) {
      lines[count++] = current;
      current = "";
    }
    // A single word too wide for the line is hard-broken rather than left to overflow.
    while (tft.textWidth(word, font) > maxWidth && word.length() > 1 && count < maxLines) {
      int fit = word.length();
      while (fit > 1 && tft.textWidth(word.substring(0, fit), font) > maxWidth) fit--;
      lines[count++] = word.substring(0, fit);
      word = word.substring(fit);
    }
    current = word;
  }
  if (!current.isEmpty() && count < maxLines) lines[count++] = current;
  return count;
}

// -- image mode -------------------------------------------------------------------------

// Draw the caption band's text in *colour*. Used both for the final draw and for each fade step,
// so the band itself is only filled once by the caller.
void paintCaptionText(const BandLayout &band, uint16_t colour) {
  tft.setTextDatum(TL_DATUM);
  tft.setTextColor(colour, kCaptionBg);
  for (uint8_t i = 0; i < band.count; ++i) {
    tft.drawString(band.lines[i], kCaptionPadX, band.top + kCaptionPadY + i * band.lineH,
                   band.font);
  }
}

// -- text mode --------------------------------------------------------------------------

struct TextLayout {
  String lines[kTextModeMaxLines];
  uint8_t count = 0;
  int16_t top = 0;
  uint8_t font = kTextModeFont;
  int16_t lineH = kTextModeLineH;
};

TextLayout layoutTextMode(const String &caption) {
  TextLayout out;
  const int16_t maxWidth = tft.width() - 2 * 12;

  // Prefer the big font; drop to the small one if the caption would not fit the screen.
  out.count = wrapText(caption, out.lines, kTextModeMaxLines, maxWidth, kTextModeFont);
  int16_t needed = out.count * kTextModeLineH;
  const int16_t available = tft.height() - kTextTop - 8;  // leave the header its room
  if (needed > available) {
    out.font = kCaptionFontSmall;
    out.lineH = kCaptionLineHSmall + 4;
    out.count = wrapText(caption, out.lines, kTextModeMaxLines, maxWidth, out.font);
    needed = out.count * out.lineH;
  }
  out.top = kTextTop + (tft.height() - kTextTop - needed) / 2;
  return out;
}

void paintTextModeCaption(const TextLayout &layout, uint16_t colour, uint16_t background) {
  tft.setTextDatum(MC_DATUM);
  tft.setTextColor(colour, background);
  for (uint8_t i = 0; i < layout.count; ++i) {
    tft.drawString(layout.lines[i], tft.width() / 2, layout.top + i * layout.lineH + layout.lineH / 2,
                   layout.font);
  }
}

// A line with thickness, stacked in both axes so a diagonal stays as thick as a straight one.
// Only ever used for the badge, which is drawn once per repaint rather than per frame.
void badgeLine(int16_t x0, int16_t y0, int16_t x1, int16_t y1, int16_t weight, uint16_t colour) {
  const int16_t half = weight / 2;
  for (int16_t ox = -half; ox <= half; ++ox) {
    for (int16_t oy = -half; oy <= half; ++oy) {
      tft.drawLine(x0 + ox, y0 + oy, x1 + ox, y1 + oy, colour);
    }
  }
}

void badgeRing(int16_t cx, int16_t cy, int16_t r, int16_t weight, uint16_t colour) {
  for (int16_t i = 0; i < weight; ++i) tft.drawCircle(cx, cy, r - i, colour);
}

// The Teams-style presence badge: a filled disc with the glyph knocked out of it.
//
// Inverted relative to Teams and to the tray icon, and it has to be: in text mode the background
// is already the status colour, so a status-coloured disc would be invisible. The disc therefore
// takes the caption's own colour and the glyph is punched out in the background colour. The
// glyph shapes themselves are the same vocabulary as make_icon() in pc_app/tray.py, scaled to r.
void drawStatusBadge(Status status, int16_t cx, int16_t cy, int16_t r, uint16_t disc,
                     uint16_t glyph) {
  tft.fillCircle(cx, cy, r, disc);
  const int16_t thick = max<int16_t>(2, r * 21 / 100);

  switch (status) {
    case Status::Available:  // tick
      badgeLine(cx - r * 43 / 100, cy + r * 4 / 100, cx - r * 11 / 100, cy + r * 39 / 100, thick,
                glyph);
      badgeLine(cx - r * 11 / 100, cy + r * 39 / 100, cx + r * 46 / 100, cy - r * 36 / 100, thick,
                glyph);
      break;
    case Status::Dnd:  // minus
      tft.fillRoundRect(cx - r / 2, cy - max<int16_t>(2, r * 14 / 100), r,
                        2 * max<int16_t>(2, r * 14 / 100), 2, glyph);
      break;
    case Status::InMeeting:  // play triangle
      tft.fillTriangle(cx - r * 21 / 100, cy - r * 43 / 100, cx - r * 21 / 100,
                       cy + r * 43 / 100, cx + r / 2, cy, glyph);
      break;
    case Status::Busy:  // solid dot
      tft.fillCircle(cx, cy, r * 36 / 100, glyph);
      break;
    case Status::Away:
    case Status::Brb: {  // clock
      const int16_t face = r / 2;
      badgeRing(cx, cy, face, max<int16_t>(2, r * 12 / 100), glyph);
      badgeLine(cx, cy, cx, cy - face * 3 / 4, max<int16_t>(2, r * 12 / 100), glyph);
      badgeLine(cx, cy, cx + face * 3 / 5, cy, max<int16_t>(2, r * 12 / 100), glyph);
      break;
    }
    case Status::Offline:  // cross
      badgeLine(cx - r * 36 / 100, cy - r * 36 / 100, cx + r * 36 / 100, cy + r * 36 / 100, thick,
                glyph);
      badgeLine(cx + r * 36 / 100, cy - r * 36 / 100, cx - r * 36 / 100, cy + r * 36 / 100, thick,
                glyph);
      break;
    default:  // Unknown / Disconnected: a hollow ring
      badgeRing(cx, cy, r * 43 / 100, max<int16_t>(2, r * 18 / 100), glyph);
      break;
  }
}

// Status colour fills the screen, the presence badge sits at the top, the caption in the middle.
void drawTextScene(Status status, const String &caption) {
  const StatusTheme &theme = themeFor(status);
  const uint16_t fg = contrastOn(theme.colour);

  tft.fillScreen(theme.colour);

  // The badge replaces the status name: the colour already says which status this is, so a word
  // for it was redundant, and a glyph reads at a glance from across the room.
  drawStatusBadge(status, tft.width() / 2, kTextBadgeCy, kTextBadgeR, fg, theme.colour);
  tft.drawFastHLine(24, kTextRuleY, tft.width() - 48, fg);

  const TextLayout layout = layoutTextMode(caption);
  if (gTransitionMs == 0) {
    paintTextModeCaption(layout, fg, theme.colour);
    return;
  }
  // Fade the caption up out of the background so a status change is not a hard cut.
  const uint16_t perStep = gTransitionMs / 2 / kFadeSteps;
  for (uint8_t step = 1; step <= kFadeSteps; ++step) {
    paintTextModeCaption(layout, lerpColour(theme.colour, fg, step, kFadeSteps), theme.colour);
    if (perStep) delay(perStep);
  }
}

// Cross-fade just the caption, leaving the rest of the screen alone. Only valid when the status
// (and therefore the background) has not changed.
void transitionTextCaption(Status status, const String &oldCaption, const String &newCaption) {
  const uint16_t bg = themeFor(status).colour;
  const uint16_t fg = contrastOn(bg);
  const uint16_t perStep = gTransitionMs ? gTransitionMs / 2 / kFadeSteps : 0;

  const TextLayout previous = layoutTextMode(oldCaption);
  if (gTransitionMs > 0 && !oldCaption.isEmpty()) {
    for (uint8_t step = 1; step <= kFadeSteps; ++step) {
      paintTextModeCaption(previous, lerpColour(fg, bg, step, kFadeSteps), bg);
      if (perStep) delay(perStep);
    }
  }
  // Clear the old text's rows before the new layout, which may use a different line count.
  const int16_t clearTop = min(previous.top, kTextTop);
  tft.fillRect(0, clearTop, tft.width(), tft.height() - clearTop, bg);

  const TextLayout next = layoutTextMode(newCaption);
  if (gTransitionMs == 0) {
    paintTextModeCaption(next, fg, bg);
    return;
  }
  for (uint8_t step = 1; step <= kFadeSteps; ++step) {
    paintTextModeCaption(next, lerpColour(bg, fg, step, kFadeSteps), bg);
    if (perStep) delay(perStep);
  }
}

// -- image mode composition ---------------------------------------------------------------

// Lay the caption band out and fill it. Shared by the blocking fade in image mode and the
// stepped one in mascot mode.
//
// Two things happen here. The font is chosen by trying the big one first and falling back to the
// small one only when the caption would not fit in its line budget. And the fill covers the
// previous band as well as the new one, so a caption that needs fewer lines than its predecessor
// does not leave the tail of it stranded above.
BandLayout layoutCaptionBand(const String &caption) {
  BandLayout band;
  if (caption.isEmpty()) return band;
  const int16_t maxWidth = tft.width() - 2 * kCaptionPadX;

  // Wrapping into more slots than the big font is allowed is how we tell "fits" from "would be
  // truncated" -- wrapText silently drops anything past the limit it is given.
  String probe[kCaptionMaxLines];
  const uint8_t big = wrapText(caption, probe, kCaptionMaxLines, maxWidth, kCaptionFontBig);
  if (big > 0 && big <= kCaptionLinesBig) {
    band.font = kCaptionFontBig;
    band.lineH = kCaptionLineHBig;
    band.count = big;
    for (uint8_t i = 0; i < big; ++i) band.lines[i] = probe[i];
  } else {
    band.font = kCaptionFontSmall;
    band.lineH = kCaptionLineHSmall;
    band.count = wrapText(caption, band.lines, kCaptionLinesSmall, maxWidth, kCaptionFontSmall);
  }
  if (band.count == 0) return band;

  band.top = tft.height() - (band.count * band.lineH + 2 * kCaptionPadY);
  const int16_t clearTop = (gBandTop >= 0 && gBandTop < band.top) ? gBandTop : band.top;
  tft.fillRect(0, clearTop, tft.width(), tft.height() - clearTop, kCaptionBg);
  gBandTop = band.top;
  return band;
}

// Called by anything that repaints the whole screen: whatever was behind the band is gone, so
// the next caption has nothing of its predecessor to cover up.
void forgetCaptionBand() { gBandTop = -1; }

void drawCaptionBand(const String &caption, bool fade) {
  const BandLayout band = layoutCaptionBand(caption);
  if (band.count == 0) return;

  if (!fade || gTransitionMs == 0) {
    paintCaptionText(band, kCaptionFg);
    return;
  }
  const uint16_t perStep = gTransitionMs / 2 / kFadeSteps;
  for (uint8_t step = 1; step <= kFadeSteps; ++step) {
    paintCaptionText(band, lerpColour(kCaptionBg, kCaptionFg, step, kFadeSteps));
    if (perStep) delay(perStep);
  }
}

// Drawn in image mode when a status has no memes flashed. Deliberately self-contained so a fresh
// board with an empty filesystem still looks like a finished product.
void drawFallbackScene(Status status, Language language) {
  const StatusTheme &theme = themeFor(status);
  const int16_t w = tft.width();
  const int16_t h = tft.height();
  const int16_t safe = h - kCaptionReserve;

  tft.fillScreen(theme.colour);

  const int16_t panelW = w - 40;
  const int16_t panelH = safe / 2;
  const int16_t panelX = 20;
  const int16_t panelY = (safe - panelH) / 2;
  tft.fillRoundRect(panelX, panelY, panelW, panelH, 10, TFT_BLACK);
  tft.fillRoundRect(panelX + 3, panelY + 3, panelW - 6, 18, 6, theme.colour);

  tft.setTextDatum(MC_DATUM);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  const char *label = labelFor(status, language);
  const uint8_t font = tft.textWidth(label, 4) <= panelW - 12 ? 4 : 2;
  tft.drawString(label, w / 2, panelY + panelH / 2 + 8, font);

  const int16_t blockW = w / 8;
  const int16_t blockY = panelY + panelH + (safe - panelY - panelH) / 2;
  for (int16_t i = 0; i < 6; ++i) {
    tft.fillRect(blockW / 2 + i * (blockW + 4), blockY, blockW, 8, TFT_BLACK);
  }
}

// Start a caption fade that advances from tick() rather than from a delay() loop.
void beginSteppedCaption(const String &caption) {
  gCaptionFade.active = false;
  gCaptionFade.band = layoutCaptionBand(caption);
  if (gCaptionFade.band.count == 0) return;

  if (gTransitionMs == 0) {
    paintCaptionText(gCaptionFade.band, kCaptionFg);
    return;
  }
  gCaptionFade.active = true;
  gCaptionFade.step = 0;
  gCaptionFade.perStepMs = gTransitionMs / 2 / kFadeSteps;
  gCaptionFade.nextMs = millis();
}

// One step of that fade. Returns true while there is more to do.
bool stepCaption(uint32_t nowMs) {
  if (!gCaptionFade.active || nowMs < gCaptionFade.nextMs) return gCaptionFade.active;
  gCaptionFade.step++;
  paintCaptionText(gCaptionFade.band,
                   lerpColour(kCaptionBg, kCaptionFg, gCaptionFade.step, kFadeSteps));
  if (gCaptionFade.step >= kFadeSteps) {
    gCaptionFade.active = false;
    return false;
  }
  gCaptionFade.nextMs = nowMs + gCaptionFade.perStepMs;
  return true;
}

// Mascot mode: the character on a dimmed status colour, with the caption band below it.
void drawMascotScene(Status status, Tone tone, const String &caption, bool captionOnly) {
  const uint32_t now = millis();
  if (!captionOnly) {
    tft.fillRect(0, 0, tft.width(), tft.height(), mascot::backdrop(status));
    forgetCaptionBand();
    mascot::reset();
    mascot::render(status, tone, now);
  }
  beginSteppedCaption(caption);
}

void drawImageScene(Status status, Language language, const String &memePath,
                    const String &caption, bool fadeCaption) {
  bool drew = false;
  if (!memePath.isEmpty() && LittleFS.exists(memePath)) {
    // A decode failure leaves a partly drawn screen, so fall back to the scene to avoid
    // showing garbage.
    drew = TJpgDec.drawFsJpg(0, 0, memePath.c_str(), LittleFS) == JDR_OK;
    if (!drew) Serial.printf("LOG:jpeg decode failed for %s\n", memePath.c_str());
  }
  if (!drew) drawFallbackScene(status, language);
  // The meme (or the fallback scene) just covered the whole screen, the old band included.
  forgetCaptionBand();
  drawCaptionBand(caption, fadeCaption);
}

}  // namespace

void begin(Orientation orientation, DisplayMode displayMode) {
  tft.init();
  gOrientation = orientation;
  gMode = displayMode;
  tft.setRotation(rotationFor(orientation));
  tft.fillScreen(TFT_BLACK);

  mascot::begin(&tft);
  mascot::layout(tft.width(), tft.height(), kCaptionReserve);

  ledcSetup(kBacklightChannel, kBacklightFreq, kBacklightBits);
  ledcAttachPin(TFT_BL, kBacklightChannel);
  setBrightness(gBrightness);

  TJpgDec.setJpgScale(1);
  TJpgDec.setSwapBytes(true);  // TFT_eSPI wants the opposite byte order to the decoder's output
  TJpgDec.setCallback(pushJpegBlock);

  Serial.printf("LOG:panel %dx%d, mode %s\n", tft.width(), tft.height(),
                displayModeName(gMode));
}

void setOrientation(Orientation newOrientation) {
  if (newOrientation == gOrientation) return;
  gOrientation = newOrientation;
  tft.setRotation(rotationFor(newOrientation));
  tft.fillScreen(TFT_BLACK);
  // The panel swapped its axes, so the character needs a new box and a new sprite.
  mascot::layout(tft.width(), tft.height(), kCaptionReserve);
  invalidate();
  Serial.printf("LOG:panel %dx%d\n", tft.width(), tft.height());
}

Orientation orientation() { return gOrientation; }

void setMode(DisplayMode newMode) {
  if (newMode == gMode) return;
  gMode = newMode;
  invalidate();
  Serial.printf("LOG:mode %s\n", displayModeName(gMode));
}

DisplayMode mode() { return gMode; }

void setTransitionMs(uint16_t ms) { gTransitionMs = ms; }

uint16_t transitionMs() { return gTransitionMs; }

void invalidate() {
  gHaveFrame = false;
  gLastCaption = "";
  gCaptionFade.active = false;
  forgetCaptionBand();
  mascot::reset();
}

void tick(Status status, Tone tone) {
  if (gMode != DisplayMode::Mascot || !gHaveFrame) return;
  const uint32_t now = millis();
  // The caption fade gets priority: it is short, and it only touches the band.
  if (stepCaption(now)) return;
  if (mascot::due(now)) mascot::render(status, tone, now);
}

int16_t width() { return tft.width(); }

int16_t height() { return tft.height(); }

void setBrightness(uint8_t percent) {
  if (percent > 100) percent = 100;
  gBrightness = percent;
  // Never fully off: a dark screen is indistinguishable from a crashed board.
  const uint32_t duty = map(percent, 0, 100, 6, 255);
  ledcWrite(kBacklightChannel, duty);
}

uint8_t brightness() { return gBrightness; }

void showFrame(Status status, Language language, Tone tone, const String &memePath,
               const String &caption) {
  // A caption-only change keeps the background and cross-fades the text in place; anything else
  // is a full repaint.
  const bool captionOnly = gHaveFrame && status == gLastStatus;

  if (gMode == DisplayMode::Mascot) {
    drawMascotScene(status, tone, caption, captionOnly);
  } else if (gMode == DisplayMode::Text) {
    if (captionOnly) {
      transitionTextCaption(status, gLastCaption, caption);
    } else {
      drawTextScene(status, caption);
    }
  } else {
    // In image mode the meme itself changes too, so the picture is always redrawn; only the
    // caption text is faded up.
    drawImageScene(status, language, memePath, caption, captionOnly);
  }

  gHaveFrame = true;
  gLastStatus = status;
  gLastCaption = caption;
  if (!gClock.isEmpty()) showClock(gClock);
}

void showClock(const String &hhmm) {
  gClock = hhmm;
  if (hhmm.isEmpty()) return;
  constexpr int16_t kW = 46, kH = 18;
  // Each mode has its own thing behind the clock: the status colour in text mode, the dimmed
  // backdrop in mascot mode, and the meme (which we cover with black) in image mode.
  uint16_t bg = TFT_BLACK;
  if (gMode == DisplayMode::Text) {
    bg = themeFor(gLastStatus).colour;
  } else if (gMode == DisplayMode::Mascot) {
    bg = mascot::backdrop(gLastStatus);
  }
  const uint16_t fg = bg == TFT_BLACK ? TFT_WHITE : contrastOn(bg);
  tft.fillRect(tft.width() - kW - 4, 4, kW, kH, bg);
  tft.setTextDatum(TR_DATUM);
  tft.setTextColor(fg, bg);
  tft.drawString(hhmm, tft.width() - 6, 6, 2);
}

}  // namespace display

#include "display.h"

#include <LittleFS.h>
#include <TFT_eSPI.h>
#include <TJpg_Decoder.h>

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
constexpr uint8_t kCaptionFont = 2;  // TFT_eSPI font 2: 16px tall, proportional
constexpr uint8_t kTextModeFont = 4;  // font 4: 26px tall, for the roomier text-only layout
constexpr int16_t kTextModeLineH = 26;

//: Steps in a fade. Enough to look continuous without making a short transition feel steppy.
constexpr uint8_t kFadeSteps = 10;

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
void paintCaptionText(const String *lines, uint8_t count, int16_t top, uint16_t colour) {
  tft.setTextDatum(TL_DATUM);
  tft.setTextColor(colour, kCaptionBg);
  for (uint8_t i = 0; i < count; ++i) {
    tft.drawString(lines[i], kCaptionPadX, top + kCaptionPadY + i * kCaptionLineH, kCaptionFont);
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
  const int16_t available = tft.height() - 64;  // leave room for the status label above
  if (needed > available) {
    out.font = kCaptionFont;
    out.lineH = kCaptionLineH + 4;
    out.count = wrapText(caption, out.lines, kTextModeMaxLines, maxWidth, out.font);
    needed = out.count * out.lineH;
  }
  out.top = 52 + (tft.height() - 52 - needed) / 2;
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

// Status colour fills the screen, the status name sits at the top, the caption in the middle.
void drawTextScene(Status status, Language language, const String &caption) {
  const StatusTheme &theme = themeFor(status);
  const uint16_t fg = contrastOn(theme.colour);

  tft.fillScreen(theme.colour);

  // Status name, with a rule under it to separate it from the caption.
  tft.setTextDatum(MC_DATUM);
  tft.setTextColor(fg, theme.colour);
  const char *label = labelFor(status, language);
  const uint8_t labelFont = tft.textWidth(label, 4) <= tft.width() - 16 ? 4 : 2;
  tft.drawString(label, tft.width() / 2, 24, labelFont);
  tft.drawFastHLine(24, 44, tft.width() - 48, fg);

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
  const int16_t clearTop = min(previous.top, static_cast<int16_t>(52));
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

void drawCaptionBand(const String &caption, bool fade) {
  if (caption.isEmpty()) return;

  String lines[kCaptionMaxLines];
  const int16_t maxWidth = tft.width() - 2 * kCaptionPadX;
  const uint8_t count = wrapText(caption, lines, kCaptionMaxLines, maxWidth, kCaptionFont);
  if (count == 0) return;

  const int16_t bandH = count * kCaptionLineH + 2 * kCaptionPadY;
  const int16_t top = tft.height() - bandH;
  tft.fillRect(0, top, tft.width(), bandH, kCaptionBg);

  if (!fade || gTransitionMs == 0) {
    paintCaptionText(lines, count, top, kCaptionFg);
    return;
  }
  const uint16_t perStep = gTransitionMs / 2 / kFadeSteps;
  for (uint8_t step = 1; step <= kFadeSteps; ++step) {
    paintCaptionText(lines, count, top, lerpColour(kCaptionBg, kCaptionFg, step, kFadeSteps));
    if (perStep) delay(perStep);
  }
}

// Drawn in image mode when a status has no memes flashed. Deliberately self-contained so a fresh
// board with an empty filesystem still looks like a finished product.
void drawFallbackScene(Status status, Language language) {
  const StatusTheme &theme = themeFor(status);
  const int16_t w = tft.width();
  const int16_t h = tft.height();
  const int16_t safe = h - (kCaptionMaxLines * kCaptionLineH + 2 * kCaptionPadY);

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
  drawCaptionBand(caption, fadeCaption);
}

}  // namespace

void begin(Orientation orientation, DisplayMode displayMode) {
  tft.init();
  gOrientation = orientation;
  gMode = displayMode;
  tft.setRotation(rotationFor(orientation));
  tft.fillScreen(TFT_BLACK);

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

void showFrame(Status status, Language language, const String &memePath, const String &caption) {
  // A caption-only change keeps the background and cross-fades the text in place; anything else
  // is a full repaint.
  const bool captionOnly = gHaveFrame && status == gLastStatus;

  if (gMode == DisplayMode::Text) {
    if (captionOnly) {
      transitionTextCaption(status, gLastCaption, caption);
    } else {
      drawTextScene(status, language, caption);
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
  const uint16_t bg = gMode == DisplayMode::Text ? themeFor(gLastStatus).colour : TFT_BLACK;
  const uint16_t fg = gMode == DisplayMode::Text ? contrastOn(bg) : TFT_WHITE;
  tft.fillRect(tft.width() - kW - 4, 4, kW, kH, bg);
  tft.setTextDatum(TR_DATUM);
  tft.setTextColor(fg, bg);
  tft.drawString(hhmm, tft.width() - 6, 6, 2);
}

}  // namespace display

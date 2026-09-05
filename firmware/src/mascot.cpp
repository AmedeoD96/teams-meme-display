#include "mascot.h"

#include <math.h>

namespace mascot {
namespace {

// Palette, matching MASCOT_* in pc_app/render.py.
constexpr uint16_t rgb565(uint32_t rgb) {
  return static_cast<uint16_t>((((rgb >> 16) & 0xFF) >> 3) << 11 | (((rgb >> 8) & 0xFF) >> 2) << 5 |
                               ((rgb & 0xFF) >> 3));
}

constexpr uint16_t kBody = rgb565(0x6264A7);
constexpr uint16_t kHighlight = rgb565(0x7B83EB);
constexpr uint16_t kShadow = rgb565(0x4B53BC);
constexpr uint16_t kPupil = rgb565(0x2B2C50);
constexpr uint16_t kBlush = rgb565(0xE87C9E);

//: Percentage of the status colour left in the backdrop. MASCOT_BACKDROP in pc_app/render.py.
constexpr uint8_t kBackdropPercent = 22;

TFT_eSPI *gPanel = nullptr;
TFT_eSprite *gSprite = nullptr;

int16_t gLeft = 0, gTop = 0, gSide = 0;
uint32_t gLastFrameMs = 0;

// Blink schedule. A blink is short and the gaps are randomised, so two boards never sync up.
uint32_t gNextBlinkMs = 0;
uint32_t gBlinkUntilMs = 0;

// -- colour helpers ---------------------------------------------------------------------

uint8_t r5(uint16_t c) { return (c >> 11) & 0x1F; }
uint8_t g6(uint16_t c) { return (c >> 5) & 0x3F; }
uint8_t b5(uint16_t c) { return c & 0x1F; }

uint16_t rgb565From(uint8_t r, uint8_t g, uint8_t b) {
  return static_cast<uint16_t>((r << 11) | (g << 5) | b);
}

uint16_t dimColour(uint16_t colour, uint8_t percent) {
  return rgb565From(r5(colour) * percent / 100, g6(colour) * percent / 100,
                    b5(colour) * percent / 100);
}

// -- drawing helpers --------------------------------------------------------------------

// A line with thickness, drawn as stacked 1px lines. drawWideLine() would do this more neatly
// but is not available in every TFT_eSPI build, and a brow is four pixels tall.
void thickLine(TFT_eSPI *g, int16_t x0, int16_t y0, int16_t x1, int16_t y1, int16_t weight,
               uint16_t colour) {
  if (weight < 1) weight = 1;
  const int16_t start = -(weight / 2);
  for (int16_t offset = start; offset < start + weight; ++offset) {
    g->drawLine(x0, y0 + offset, x1, y1 + offset, colour);
  }
}

// A quadratic curve through (x0,y) -> (cx,cy) -> (x1,y), flattened into segments. Used for the
// mouth, so a smile and a frown are the same code with the control point moved.
void curve(TFT_eSPI *g, int16_t x0, int16_t y0, int16_t cx, int16_t cy, int16_t x1, int16_t y1,
           int16_t weight, uint16_t colour) {
  constexpr uint8_t kSegments = 10;
  int16_t px = x0, py = y0;
  for (uint8_t i = 1; i <= kSegments; ++i) {
    const float t = static_cast<float>(i) / kSegments;
    const float inv = 1.0f - t;
    const int16_t nx = static_cast<int16_t>(inv * inv * x0 + 2 * inv * t * cx + t * t * x1);
    const int16_t ny = static_cast<int16_t>(inv * inv * y0 + 2 * inv * t * cy + t * t * y1);
    thickLine(g, px, py, nx, ny, weight, colour);
    px = nx;
    py = ny;
  }
}

// -- animation --------------------------------------------------------------------------

struct Motion {
  int16_t dx = 0;
  int16_t dy = 0;
  //: Scales the eye height, so a blink is the same code path as a half-lidded expression.
  uint8_t eyeScale = 100;
};

Motion motionFor(Idle idle, uint32_t nowMs) {
  Motion m;
  const float t = nowMs / 1000.0f;
  switch (idle) {
    case Idle::Bob:
      m.dy = static_cast<int16_t>(sinf(t * 1.6f) * 4.0f);
      break;
    case Idle::Bounce:
      // Always above the resting line, so it reads as hopping rather than sinking.
      m.dy = static_cast<int16_t>(-fabsf(sinf(t * 3.4f)) * 6.0f);
      break;
    case Idle::Sway:
      m.dx = static_cast<int16_t>(sinf(t * 1.1f) * 4.0f);
      break;
    case Idle::Slump:
      // Sits low, with a slow sigh every few seconds.
      m.dy = static_cast<int16_t>(4.0f + sinf(t * 0.5f) * 2.0f);
      break;
    case Idle::Sleep:
      m.dy = static_cast<int16_t>(sinf(t * 0.7f) * 2.0f);
      break;
    case Idle::Twitch: {
      // Still most of the time, with an occasional flinch.
      const uint32_t phase = nowMs % 2500;
      if (phase < 160) m.dx = (phase / 40) % 2 ? 3 : -3;
      break;
    }
    default:
      break;
  }
  return m;
}

void scheduleBlink(uint32_t nowMs) { gNextBlinkMs = nowMs + random(3000, 6000); }

uint8_t blinkScale(Idle idle, uint32_t nowMs) {
  if (idle == Idle::Sleep) return 100;  // already shut; a blink would be invisible
  if (gNextBlinkMs == 0) scheduleBlink(nowMs);
  if (nowMs >= gBlinkUntilMs && nowMs >= gNextBlinkMs) {
    gBlinkUntilMs = nowMs + 120;
    scheduleBlink(nowMs + 120);
  }
  return nowMs < gBlinkUntilMs ? 0 : 100;
}

// -- the character ----------------------------------------------------------------------

// Draw the character with its top-left at (ox, oy) in *g*'s coordinates, *side* pixels across.
// Every coordinate is a fraction of *side*, the same fractions pc_app/render.py uses.
void drawFace(TFT_eSPI *g, int16_t ox, int16_t oy, int16_t side, const Face &face,
              const Motion &motion, uint16_t badge) {
  const int16_t left = ox + motion.dx;
  const int16_t top = oy + motion.dy;
  auto fx = [&](float f) { return static_cast<int16_t>(left + f * side); };
  auto fy = [&](float f) { return static_cast<int16_t>(top + f * side); };
  auto fs = [&](float f) { return static_cast<int16_t>(f * side); };

  g->fillRoundRect(left, top, side, side, fs(0.28f), kBody);
  // A lighter band across the top reads as a light source without needing a gradient.
  g->fillRoundRect(fx(0.06f), fy(0.05f), fs(0.88f), fs(0.31f), fs(0.18f), kHighlight);

  // The T mark. Evokes Teams without reproducing its logo.
  g->fillRect(fx(0.30f), fy(0.09f), fs(0.40f), fs(0.055f), TFT_WHITE);
  g->fillRect(fx(0.465f), fy(0.09f), fs(0.07f), fs(0.16f), TFT_WHITE);

  // Eyes. The height carries both the expression and the blink.
  const int16_t eyeRx = fs(0.085f);
  int16_t eyeRy = static_cast<int16_t>(eyeRx * face.eyeOpen / 100 * motion.eyeScale / 100);
  if (eyeRy < 1) eyeRy = 1;
  const int16_t eyeY = fy(0.47f);
  const int16_t pupilR = min(static_cast<int16_t>(eyeRx * 0.55f),
                             static_cast<int16_t>(eyeRy * 0.85f));
  for (uint8_t i = 0; i < 2; ++i) {
    const int16_t cx = fx(i == 0 ? 0.34f : 0.66f);
    g->fillEllipse(cx, eyeY, eyeRx, eyeRy, TFT_WHITE);
    if (face.eyeOpen > 12 && motion.eyeScale > 0 && pupilR > 0) {
      g->fillCircle(cx, eyeY, pupilR, kPupil);
    }
  }

  // Brows. A negative tilt drops the inner ends towards the nose, which is what reads as
  // furrowed; browAsym flattens the left one so only the right lifts.
  const int16_t browHalf = fs(0.08f);
  const int16_t browY = fy(0.33f);
  const int16_t browWeight = max<int16_t>(2, fs(0.03f));
  for (uint8_t i = 0; i < 2; ++i) {
    const int16_t cx = fx(i == 0 ? 0.34f : 0.66f);
    const int16_t tilt = (face.browAsym && i == 0) ? -face.browTilt / 3 : face.browTilt;
    const int16_t lift = static_cast<int16_t>(tilt / 100.0f * fs(0.05f));
    const int16_t innerY = browY - lift;
    const int16_t outerY = browY + lift;
    // The left brow's inner end is the one on its right, and vice versa.
    const int16_t leftY = (i == 0) ? outerY : innerY;
    const int16_t rightY = (i == 0) ? innerY : outerY;
    thickLine(g, cx - browHalf, leftY, cx + browHalf, rightY, browWeight, kShadow);
  }

  if (face.blush) {
    const int16_t br = fs(0.06f);
    for (uint8_t i = 0; i < 2; ++i) {
      g->fillEllipse(fx(i == 0 ? 0.22f : 0.78f), fy(0.60f), br,
                     max<int16_t>(1, static_cast<int16_t>(br * 0.6f)), kBlush);
    }
  }

  // Mouth.
  const int16_t mouthCx = fx(0.5f);
  const int16_t mouthCy = fy(0.68f);
  const int16_t mouthHalf = fs(0.13f);
  const int16_t mouthWeight = max<int16_t>(2, fs(0.028f));
  const int16_t bend = static_cast<int16_t>(face.mouthCurve / 100.0f * fs(0.16f));
  if (face.mouthOpen > 0) {
    const int16_t oh = fs(0.02f) + fs(0.05f) * face.mouthOpen / 100;
    g->fillEllipse(mouthCx, mouthCy + bend / 4, static_cast<int16_t>(mouthHalf * 0.8f),
                   max<int16_t>(1, oh / 2), kPupil);
  } else {
    curve(g, mouthCx - mouthHalf, mouthCy, mouthCx, mouthCy + bend, mouthCx + mouthHalf, mouthCy,
          mouthWeight, kPupil);
  }

  // Presence badge, echoing the dot Teams puts on your avatar. This is what keeps the real
  // status readable when the tone has the face saying something else.
  const int16_t badgeX = fx(0.87f);
  const int16_t badgeY = fy(0.87f);
  const int16_t badgeR = fs(0.14f);
  g->fillCircle(badgeX, badgeY, badgeR, TFT_WHITE);
  g->fillCircle(badgeX, badgeY, static_cast<int16_t>(badgeR * 0.72f), badge);
}

}  // namespace

void begin(TFT_eSPI *panel) {
  gPanel = panel;
  reset();
}

void layout(int16_t width, int16_t height, int16_t captionReserve) {
  const int16_t usableH = height - captionReserve;
  int16_t side = min(width, usableH) - 2 * kMargin;
  if (side > kMaxSize) side = kMaxSize;
  if (side < 60) side = 60;

  gSide = side;
  gLeft = (width - side) / 2;
  gTop = (usableH - side) / 2;

  // The sprite is grown by the motion allowance on every side so a bob is never clipped.
  const int16_t spriteSide = side + 2 * kMotion;
  if (gSprite != nullptr) {
    gSprite->deleteSprite();
    delete gSprite;
    gSprite = nullptr;
  }
  if (gPanel != nullptr) {
    gSprite = new TFT_eSprite(gPanel);
    gSprite->setColorDepth(16);
    if (gSprite->createSprite(spriteSide, spriteSide) == nullptr) {
      // Not enough heap: fall back to drawing straight to the panel. It flickers a little, which
      // is much better than not booting.
      delete gSprite;
      gSprite = nullptr;
      Serial.printf("LOG:no heap for a %dx%d mascot sprite, drawing direct\n", spriteSide,
                    spriteSide);
    }
  }
  reset();
}

uint16_t backdrop(Status status) { return dimColour(themeFor(status).colour, kBackdropPercent); }

bool due(uint32_t nowMs) { return nowMs - gLastFrameMs >= kFrameMs; }

bool degraded() { return gSprite == nullptr; }

void reset() {
  gLastFrameMs = 0;
  gNextBlinkMs = 0;
  gBlinkUntilMs = 0;
}

void render(Status status, Tone tone, uint32_t nowMs) {
  if (gPanel == nullptr || gSide == 0) return;

  const uint8_t statusIndex = static_cast<uint8_t>(status);
  const uint8_t toneIndex = static_cast<uint8_t>(tone);
  const Face &face = kFaces[statusIndex < kFaceStatusCount ? statusIndex : 0]
                           [toneIndex < kFaceToneCount ? toneIndex : 0];

  Motion motion = motionFor(face.idle, nowMs);
  motion.eyeScale = blinkScale(face.idle, nowMs);

  const uint16_t back = backdrop(status);
  const uint16_t badge = themeFor(status).colour;

  if (gSprite != nullptr) {
    gSprite->fillSprite(back);
    drawFace(gSprite, kMotion, kMotion, gSide, face, motion, badge);
    gSprite->pushSprite(gLeft - kMotion, gTop - kMotion);
  } else {
    // No sprite: clear the whole motion box first, or the previous frame leaves a trail.
    gPanel->fillRect(gLeft - kMotion, gTop - kMotion, gSide + 2 * kMotion, gSide + 2 * kMotion,
                     back);
    drawFace(gPanel, gLeft, gTop, gSide, face, motion, badge);
  }
  gLastFrameMs = nowMs;
}

}  // namespace mascot

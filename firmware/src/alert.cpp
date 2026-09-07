#include "alert.h"

#include "serial_link.h"

#include <AnimatedGIF.h>
#include <LittleFS.h>
#include <mbedtls/base64.h>

namespace alert {
namespace {

AnimatedGIF gGif;
TFT_eSPI *gPanel = nullptr;
// fs::File rather than plain File: TFT_eSPI defines FS_NO_GLOBALS, so the unqualified alias
// FS.h normally provides does not exist in any translation unit that includes it.
fs::File gFile;

bool gActive = false;
uint32_t gDeadlineMs = 0;
uint32_t gNextFrameMs = 0;
int16_t gLeft = 0;
int16_t gTop = 0;

//: One scanline of the widest panel orientation, converted to RGB565 before it is pushed.
constexpr int kMaxLineWidth = 320;

//: A GIF that asks for a 0 ms delay would otherwise spin the loop flat out, starving the serial
//: poll. 20 ms is 50 fps, which is faster than the panel can be filled anyway.
constexpr int kMinFrameMs = 20;

// -- upload state -------------------------------------------------------------------------

fs::File gUploadFile;
bool gUploading = false;
uint32_t gExpectedBytes = 0;
uint32_t gExpectedCrc = 0;
uint32_t gReceivedBytes = 0;
uint32_t gRunningCrc = 0;
uint16_t gChunksSinceAck = 0;
uint32_t gLastChunkMs = 0;

// Written out rather than taken from ESP-IDF's crc32_le, whose initial-value and inversion
// convention is easy to get subtly wrong. This is the plain reflected CRC-32 (0xEDB88320) that
// zlib computes, which is what pc_app/gif_upload.py sends.
uint32_t crc32Update(uint32_t crc, const uint8_t *data, size_t length) {
  crc = ~crc;
  while (length--) {
    crc ^= *data++;
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc >> 1) ^ (0xEDB88320u & (~(crc & 1u) + 1u));
    }
  }
  return ~crc;
}

void uploadFail(const char *reason) {
  if (gUploadFile) gUploadFile.close();
  LittleFS.remove(kGifTempPath);
  gUploading = false;
  Serial.printf("EVT:GIFERR:%s\n", reason);
}

// -- LittleFS file callbacks --------------------------------------------------------------

void *openFile(const char *path, int32_t *size) {
  gFile = LittleFS.open(path, "r");
  if (!gFile) return nullptr;
  *size = static_cast<int32_t>(gFile.size());
  return &gFile;
}

void closeFile(void *handle) {
  fs::File *file = static_cast<fs::File *>(handle);
  if (file) file->close();
}

int32_t readFile(GIFFILE *page, uint8_t *buffer, int32_t length) {
  fs::File *file = static_cast<fs::File *>(page->fHandle);
  // Stopping one byte short of the end is the library's documented work-around: reading a file
  // all the way to its last byte leaves seek() unable to rewind, and playFrame() rewinds to loop.
  if ((page->iSize - page->iPos) < length) length = page->iSize - page->iPos - 1;
  if (length <= 0) return 0;
  const int32_t read = static_cast<int32_t>(file->read(buffer, length));
  page->iPos = static_cast<int32_t>(file->position());
  return read;
}

int32_t seekFile(GIFFILE *page, int32_t position) {
  fs::File *file = static_cast<fs::File *>(page->fHandle);
  file->seek(position);
  page->iPos = static_cast<int32_t>(file->position());
  return page->iPos;
}

// -- drawing ------------------------------------------------------------------------------

// One decoded scanline, palette-mapped and pushed straight at the panel. No framebuffer, which
// is the whole reason this works on a board with 4MB of flash and no PSRAM.
void drawLine(GIFDRAW *draw) {
  if (gPanel == nullptr) return;

  const int16_t y = gTop + draw->iY + draw->y;
  if (y < 0 || y >= gPanel->height()) return;

  int width = draw->iWidth;
  if (width > kMaxLineWidth) width = kMaxLineWidth;
  const int16_t x0 = gLeft + draw->iX;

  uint16_t line[kMaxLineWidth];
  const uint16_t *palette = draw->pPalette;
  const uint8_t *pixels = draw->pPixels;

  if (!draw->ucHasTransparency) {
    for (int x = 0; x < width; ++x) line[x] = palette[pixels[x]];
    gPanel->pushImage(x0, y, width, 1, line);
    return;
  }

  // Transparent pixels have to leave what is underneath alone, so the line goes out as runs of
  // opaque pixels instead of one push. tools/build_memes.py re-encodes uploads without any
  // transparency at all, so this is the path only a hand-flashed GIF takes.
  int x = 0;
  while (x < width) {
    while (x < width && pixels[x] == draw->ucTransparent) x++;
    const int start = x;
    int count = 0;
    while (x < width && pixels[x] != draw->ucTransparent) line[count++] = palette[pixels[x++]];
    if (count > 0) gPanel->pushImage(x0 + start, y, count, 1, line);
  }
}

}  // namespace

void begin(TFT_eSPI *panel) { gPanel = panel; }

// -- upload ---------------------------------------------------------------------------------

void uploadBegin(uint32_t bytes, uint32_t crc) {
  if (gUploading) uploadFail("restarted");
  if (bytes == 0 || bytes > kMaxGifBytes) {
    Serial.printf("EVT:GIFERR:size %lu\n", static_cast<unsigned long>(bytes));
    return;
  }
  // The old GIF still occupies its own space until the rename, so both have to fit at once.
  const uint32_t free = LittleFS.totalBytes() - LittleFS.usedBytes();
  if (bytes + 4096 > free) {
    Serial.printf("EVT:GIFERR:no space (%lu free)\n", static_cast<unsigned long>(free));
    return;
  }

  // Stop playing before touching the file: the decoder is reading it.
  stop();
  LittleFS.remove(kGifTempPath);
  gUploadFile = LittleFS.open(kGifTempPath, "w");
  if (!gUploadFile) {
    Serial.println(F("EVT:GIFERR:cannot open temp file"));
    return;
  }

  gExpectedBytes = bytes;
  gExpectedCrc = crc;
  gReceivedBytes = 0;
  gRunningCrc = 0;
  gChunksSinceAck = 0;
  gLastChunkMs = millis();
  gUploading = true;
  Serial.printf("LOG:upload %lu bytes\n", static_cast<unsigned long>(bytes));
  // An immediate ack releases the first window without waiting for it to be filled.
  Serial.println(F("EVT:GIFACK:0"));
}

void uploadChunk(const String &encoded) {
  if (!gUploading) return;
  gLastChunkMs = millis();

  // 3 bytes out per 4 characters in, so the chunk cannot exceed the line limit's worth.
  // Sized from the protocol's own line limit, so the two cannot drift apart.
  uint8_t raw[(serial_link::kMaxLine / 4) * 3];
  size_t decoded = 0;
  const int rc = mbedtls_base64_decode(raw, sizeof(raw), &decoded,
                                       reinterpret_cast<const unsigned char *>(encoded.c_str()),
                                       encoded.length());
  if (rc != 0) {
    uploadFail("bad base64");
    return;
  }
  if (gReceivedBytes + decoded > gExpectedBytes) {
    uploadFail("too long");
    return;
  }
  if (gUploadFile.write(raw, decoded) != decoded) {
    uploadFail("write failed");
    return;
  }
  gRunningCrc = crc32Update(gRunningCrc, raw, decoded);
  gReceivedBytes += decoded;

  if (++gChunksSinceAck >= kUploadAckEvery) {
    gChunksSinceAck = 0;
    Serial.printf("EVT:GIFACK:%lu\n", static_cast<unsigned long>(gReceivedBytes));
  }
}

void uploadEnd() {
  if (!gUploading) {
    Serial.println(F("EVT:GIFERR:no upload in progress"));
    return;
  }
  gUploadFile.close();
  gUploading = false;

  if (gReceivedBytes != gExpectedBytes) {
    LittleFS.remove(kGifTempPath);
    Serial.printf("EVT:GIFERR:short (%lu of %lu)\n", static_cast<unsigned long>(gReceivedBytes),
                  static_cast<unsigned long>(gExpectedBytes));
    return;
  }
  if (gRunningCrc != gExpectedCrc) {
    LittleFS.remove(kGifTempPath);
    Serial.println(F("EVT:GIFERR:checksum"));
    return;
  }

  // Only now is the previous GIF given up. Everything above this line leaves it playable.
  LittleFS.remove(kGifPath);
  if (!LittleFS.rename(kGifTempPath, kGifPath)) {
    LittleFS.remove(kGifTempPath);
    Serial.println(F("EVT:GIFERR:rename failed"));
    return;
  }
  Serial.printf("EVT:GIFOK:%lu\n", static_cast<unsigned long>(gReceivedBytes));
}

bool uploading() { return gUploading; }

bool play(uint32_t durationMs) {
  if (gPanel == nullptr || durationMs == 0) return false;
  if (gUploading) {
    // The file is being rewritten underneath us.
    Serial.println(F("EVT:ALERTERR:uploading"));
    return false;
  }
  if (gActive) stop();  // a second ALERT restarts cleanly rather than stacking

  if (!LittleFS.exists(kGifPath)) {
    Serial.println(F("EVT:ALERTERR:nogif"));
    Serial.println(F("LOG:no /alert.gif -- flash the pack or upload one from the settings window"));
    return false;
  }

  // Big-endian palette entries with TFT_eSPI's byte swap left off, which is the same byte order
  // the JPEG path arrives in (see TJpgDec.setSwapBytes in display.cpp).
  gGif.begin(GIF_PALETTE_RGB565_BE);
  if (!gGif.open(kGifPath, openFile, closeFile, readFile, seekFile, drawLine)) {
    Serial.printf("EVT:ALERTERR:decode %d\n", gGif.getLastError());
    Serial.printf("LOG:could not open %s (gif error %d)\n", kGifPath, gGif.getLastError());
    return false;
  }

  const int width = gGif.getCanvasWidth();
  const int height = gGif.getCanvasHeight();
  // Centred, so one asset serves both orientations without a per-orientation folder.
  gLeft = static_cast<int16_t>((gPanel->width() - width) / 2);
  gTop = static_cast<int16_t>((gPanel->height() - height) / 2);
  // The GIF need not cover the panel, and what is behind it is the status screen.
  gPanel->fillScreen(TFT_BLACK);

  const uint32_t now = millis();
  gDeadlineMs = now + durationMs;
  gNextFrameMs = now;
  gActive = true;
  // Answered on the wire and not only in the log: the settings window's test button has no
  // other way to tell a playing alert apart from a board that never understood the command.
  Serial.printf("EVT:ALERT:%lu\n", static_cast<unsigned long>(durationMs));
  Serial.printf("LOG:alert %dx%d for %lums\n", width, height, static_cast<unsigned long>(durationMs));
  return true;
}

bool active() { return gActive; }

bool tick() {
  const uint32_t now = millis();

  // A transfer that stops mid-flight must not hold the state (and the temp file) forever.
  if (gUploading && static_cast<int32_t>(now - gLastChunkMs) > static_cast<int32_t>(kUploadTimeoutMs)) {
    uploadFail("timeout");
  }

  if (!gActive) return false;

  // Signed comparison so a millis() rollover does not strand the GIF on screen.
  if (static_cast<int32_t>(now - gDeadlineMs) >= 0) {
    stop();
    return true;
  }
  if (static_cast<int32_t>(now - gNextFrameMs) < 0) return false;

  // bSync false: the library must not sleep out the inter-frame delay for us, or serial_link
  // stops being polled for the length of it. We schedule the next frame ourselves instead.
  int frameDelayMs = 0;
  if (gGif.playFrame(false, &frameDelayMs) < 0) {
    Serial.printf("LOG:gif decode failed (error %d)\n", gGif.getLastError());
    stop();
    return true;
  }
  if (frameDelayMs < kMinFrameMs) frameDelayMs = kMinFrameMs;
  gNextFrameMs = now + static_cast<uint32_t>(frameDelayMs);
  return false;
}

void stop() {
  if (!gActive) return;
  gGif.close();
  gActive = false;
}

}  // namespace alert

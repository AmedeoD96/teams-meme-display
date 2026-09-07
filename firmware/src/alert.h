// The out-of-hours alert: an animated GIF played over whatever is on screen.
//
// The PC decides *when* -- it is the side that knows your Teams notifications and your working
// hours (pc_app/work_hours.py) -- and sends ALERT:<ms>. The board only knows how to play the
// thing. See docs/PROTOCOL.md.
//
// GIF decoding fits on a board with no PSRAM for the same reason JPEG does: AnimatedGIF hands
// back one scanline at a time through a callback, so there is never a framebuffer. That is the
// identical trick pushJpegBlock() plays in display.cpp.
#pragma once

#include <Arduino.h>
#include <TFT_eSPI.h>

namespace alert {

//: Where the GIF lives on LittleFS. Flashed by tools/build_memes.py, and replaced in place by an
//: upload from the settings window.
constexpr char kGifPath[] = "/alert.gif";

//: An upload writes here first and is renamed over kGifPath only once it verifies, so a cable
//: pulled mid-transfer leaves the previous GIF playable.
constexpr char kGifTempPath[] = "/alert.gif.tmp";

//: Refuse an upload larger than this. At 115200 with base64 framing it is already ~35s.
constexpr uint32_t kMaxGifBytes = 300u * 1024u;

void begin(TFT_eSPI *panel);

// Start playing kGifPath for *durationMs*. Returns false and changes nothing on screen if there
// is no GIF to play or it cannot be decoded.
bool play(uint32_t durationMs);

// True while the GIF owns the panel. Nothing else may draw until this goes false.
bool active();

// Decode one frame if one is due. Returns true on the tick the alert *ends*, which is the
// caller's cue to repaint the status display. Call every loop; cheap when inactive.
bool tick();

void stop();

// -- upload ---------------------------------------------------------------------------------
//
// GIFBEGIN / GIFDATA / GIFEND, base64 over the ordinary line protocol so serial_link's framing
// is untouched. Bytes land in kGifTempPath and are only renamed over kGifPath once the length
// and CRC32 both check out, so a cable pulled mid-transfer costs the new GIF and not the old one.

//: ACK interval, in chunks. The PC sends this many and then waits, which is what keeps the
//: transfer inside the serial RX buffer while LittleFS is being written.
constexpr uint16_t kUploadAckEvery = 16;

//: Give up on a transfer that stalls, so a PC that died mid-upload does not strand the state.
constexpr uint32_t kUploadTimeoutMs = 10000;

// Start receiving *bytes* whose CRC32 should come to *crc*. Replies EVT:GIFERR and receives
// nothing if the size is implausible or the file cannot be opened.
void uploadBegin(uint32_t bytes, uint32_t crc);

// One base64 chunk. Silently ignored when no upload is in progress.
void uploadChunk(const String &encoded);

// Verify and commit. Replies EVT:GIFOK or EVT:GIFERR:<reason>.
void uploadEnd();

// True while a transfer is in progress; playback is refused meanwhile.
bool uploading();

}  // namespace alert

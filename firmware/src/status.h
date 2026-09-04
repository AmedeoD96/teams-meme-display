// Status enum, caption language and screen orientation, with per-status theming.
// Must agree with docs/PROTOCOL.md, pc_app/presence.py and tools/build_memes.py.
#pragma once

#include <Arduino.h>

enum class Status : uint8_t {
  Available = 0,
  Busy,
  InMeeting,
  Dnd,
  Away,
  Brb,
  Offline,
  Unknown,
  Disconnected,
  Count,
};

enum class Language : uint8_t {
  En = 0,
  It,
  Count,
};

enum class Orientation : uint8_t {
  Landscape = 0,
  Portrait,
  Count,
};

//: How a frame is composed: a meme with a caption band, or the caption alone on the status colour.
enum class DisplayMode : uint8_t {
  Image = 0,
  Text,
  Count,
};

constexpr uint8_t kStatusCount = static_cast<uint8_t>(Status::Count);
constexpr uint8_t kLanguageCount = static_cast<uint8_t>(Language::Count);
constexpr uint8_t kOrientationCount = static_cast<uint8_t>(Orientation::Count);
constexpr uint8_t kDisplayModeCount = static_cast<uint8_t>(DisplayMode::Count);

struct StatusTheme {
  const char *token;   // wire token, e.g. "IN_MEETING"
  const char *folder;  // LittleFS folder / caption file stem, e.g. "in_meeting"
  const char *labels[kLanguageCount];  // shown on the fallback scene, indexed by Language
  uint16_t colour;     // RGB565 accent, matching STATUS_COLOR in pc_app/presence.py
};

const StatusTheme &themeFor(Status status);

// Parse wire tokens. Each returns false and leaves *out untouched if the token is unknown.
bool statusFromToken(const String &token, Status *out);
bool languageFromToken(const String &token, Language *out);
bool orientationFromToken(const String &token, Orientation *out);
bool displayModeFromToken(const String &token, DisplayMode *out);

inline const char *statusToken(Status status) { return themeFor(status).token; }

const char *labelFor(Status status, Language language);

// "en" / "it" -- also the caption folder name on the filesystem.
const char *languageCode(Language language);

// "land" / "port" -- the meme folder name on the filesystem.
const char *orientationFolder(Orientation orientation);

// "image" / "text".
const char *displayModeName(DisplayMode mode);

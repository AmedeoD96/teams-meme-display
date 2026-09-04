#include "status.h"

namespace {

// Written as 24-bit hex so the values can be compared by eye with STATUS_COLOR in
// pc_app/presence.py and status_colour() in tools/build_memes.py.
constexpr uint16_t rgb565(uint32_t rgb) {
  return static_cast<uint16_t>((((rgb >> 16) & 0xFF) >> 3) << 11 | (((rgb >> 8) & 0xFF) >> 2) << 5 |
                               ((rgb & 0xFF) >> 3));
}

// Indexed by Status, so the order here must match the enum. Labels are indexed by Language.
// Italian is written without accents: the display font (TFT_eSPI font 2) is ASCII only.
const StatusTheme kThemes[kStatusCount] = {
    {"AVAILABLE", "available", {"AVAILABLE", "DISPONIBILE"}, rgb565(0x2ECC71)},
    {"BUSY", "busy", {"BUSY", "OCCUPATO"}, rgb565(0xE74C3C)},
    {"IN_MEETING", "in_meeting", {"IN A MEETING", "IN RIUNIONE"}, rgb565(0x8E44AD)},
    {"DND", "dnd", {"DO NOT DISTURB", "NON DISTURBARE"}, rgb565(0xB03A2E)},
    {"AWAY", "away", {"AWAY", "ASSENTE"}, rgb565(0xF39C12)},
    {"BRB", "brb", {"BE RIGHT BACK", "TORNO SUBITO"}, rgb565(0xE67E22)},
    {"OFFLINE", "offline", {"OFFLINE", "NON IN LINEA"}, rgb565(0x7F8C8D)},
    {"UNKNOWN", "unknown", {"UNKNOWN", "SCONOSCIUTO"}, rgb565(0x566573)},
    {"DISCONNECTED", "disconnected", {"NO PC", "NESSUN PC"}, rgb565(0x34495E)},
};

const char *const kLanguageCodes[kLanguageCount] = {"en", "it"};
const char *const kOrientationFolders[kOrientationCount] = {"land", "port"};

// Accepted spellings for ORIENT:, indexed to match Orientation.
const char *const kOrientationTokens[kOrientationCount][2] = {
    {"LANDSCAPE", "LAND"},
    {"PORTRAIT", "PORT"},
};

const char *const kDisplayModeNames[kDisplayModeCount] = {"image", "text"};

}  // namespace

const StatusTheme &themeFor(Status status) {
  const uint8_t index = static_cast<uint8_t>(status);
  return kThemes[index < kStatusCount ? index : static_cast<uint8_t>(Status::Unknown)];
}

bool statusFromToken(const String &token, Status *out) {
  for (uint8_t i = 0; i < kStatusCount; ++i) {
    if (token.equalsIgnoreCase(kThemes[i].token)) {
      *out = static_cast<Status>(i);
      return true;
    }
  }
  return false;
}

bool languageFromToken(const String &token, Language *out) {
  for (uint8_t i = 0; i < kLanguageCount; ++i) {
    if (token.equalsIgnoreCase(kLanguageCodes[i])) {
      *out = static_cast<Language>(i);
      return true;
    }
  }
  return false;
}

bool orientationFromToken(const String &token, Orientation *out) {
  for (uint8_t i = 0; i < kOrientationCount; ++i) {
    for (const char *spelling : kOrientationTokens[i]) {
      if (token.equalsIgnoreCase(spelling)) {
        *out = static_cast<Orientation>(i);
        return true;
      }
    }
  }
  return false;
}

bool displayModeFromToken(const String &token, DisplayMode *out) {
  for (uint8_t i = 0; i < kDisplayModeCount; ++i) {
    if (token.equalsIgnoreCase(kDisplayModeNames[i])) {
      *out = static_cast<DisplayMode>(i);
      return true;
    }
  }
  return false;
}

const char *displayModeName(DisplayMode mode) {
  const uint8_t index = static_cast<uint8_t>(mode);
  return kDisplayModeNames[index < kDisplayModeCount ? index : 0];
}

const char *labelFor(Status status, Language language) {
  const uint8_t index = static_cast<uint8_t>(language);
  return themeFor(status).labels[index < kLanguageCount ? index : 0];
}

const char *languageCode(Language language) {
  const uint8_t index = static_cast<uint8_t>(language);
  return kLanguageCodes[index < kLanguageCount ? index : 0];
}

const char *orientationFolder(Orientation orientation) {
  const uint8_t index = static_cast<uint8_t>(orientation);
  return kOrientationFolders[index < kOrientationCount ? index : 0];
}

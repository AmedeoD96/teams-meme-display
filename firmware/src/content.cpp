#include "content.h"

#include <LittleFS.h>

namespace content {
namespace {

bool gMounted = false;
Orientation gOrientation = Orientation::Landscape;
Language gLanguage = Language::En;

struct StatusContent {
  uint8_t memeCount = 0;
  int16_t lastMeme = -1;
  int16_t lastCaption = -1;
};

StatusContent gContent[kStatusCount];

String memeDir(Status status) {
  return String("/memes/") + orientationFolder(gOrientation) + "/" + themeFor(status).folder;
}

String captionPath(Status status) {
  return String("/captions/") + languageCode(gLanguage) + "/" + themeFor(status).folder + ".txt";
}

// Count non-empty lines without holding the file in memory.
uint16_t countLines(const String &path) {
  File file = LittleFS.open(path, "r");
  if (!file) return 0;
  uint16_t count = 0;
  while (file.available()) {
    String line = file.readStringUntil('\n');
    line.trim();
    if (!line.isEmpty()) count++;
  }
  file.close();
  return count;
}

// Return line number *wanted* (0-based, blank lines skipped), or "" if there is no such line.
String readLine(const String &path, uint16_t wanted) {
  File file = LittleFS.open(path, "r");
  if (!file) return String();
  uint16_t index = 0;
  while (file.available()) {
    String line = file.readStringUntil('\n');
    line.trim();
    if (line.isEmpty()) continue;
    if (index++ == wanted) {
      file.close();
      return line;
    }
  }
  file.close();
  return String();
}

// Pick an index in [0, count) that is not *previous*, so the same meme does not come up twice
// in a row. With a single item there is no choice to make.
uint16_t pickDifferent(uint16_t count, int16_t previous) {
  if (count <= 1) return 0;
  uint16_t choice = random(count);
  if (choice == static_cast<uint16_t>(previous)) choice = (choice + 1) % count;
  return choice;
}

void rescan() {
  for (uint8_t i = 0; i < kStatusCount; ++i) {
    const Status status = static_cast<Status>(i);
    const uint16_t count = gMounted ? countLines(memeDir(status) + "/index.txt") : 0;
    gContent[i].memeCount = count > 255 ? 255 : static_cast<uint8_t>(count);
    gContent[i].lastMeme = -1;  // indexes are per-orientation, so the old pick means nothing
  }
}

}  // namespace

bool begin(Orientation orientation, Language language) {
  gOrientation = orientation;
  gLanguage = language;
  gMounted = LittleFS.begin(false);
  if (!gMounted) {
    Serial.println(F("LOG:no LittleFS -- run 'pio run -t uploadfs' to flash the meme pack"));
    return false;
  }
  rescan();
  return true;
}

bool mounted() { return gMounted; }

void setOrientation(Orientation orientation) {
  if (orientation == gOrientation) return;
  gOrientation = orientation;
  rescan();
  Serial.printf("LOG:orientation %s, %u memes\n", orientationFolder(gOrientation), totalMemes());
}

void setLanguage(Language language) {
  if (language == gLanguage) return;
  gLanguage = language;
  for (uint8_t i = 0; i < kStatusCount; ++i) gContent[i].lastCaption = -1;
  Serial.printf("LOG:language %s\n", languageCode(gLanguage));
}

Orientation orientation() { return gOrientation; }

Language language() { return gLanguage; }

uint8_t memeCount(Status status) { return gContent[static_cast<uint8_t>(status)].memeCount; }

uint16_t totalMemes() {
  uint16_t total = 0;
  for (uint8_t i = 0; i < kStatusCount; ++i) total += gContent[i].memeCount;
  return total;
}

String nextMeme(Status status) {
  StatusContent &state = gContent[static_cast<uint8_t>(status)];
  if (!gMounted || state.memeCount == 0) return String();

  const uint16_t choice = pickDifferent(state.memeCount, state.lastMeme);
  state.lastMeme = static_cast<int16_t>(choice);

  const String dir = memeDir(status);
  const String name = readLine(dir + "/index.txt", choice);
  if (name.isEmpty()) return String();
  return dir + "/" + name;
}

String nextCaption(Status status) {
  StatusContent &state = gContent[static_cast<uint8_t>(status)];
  if (gMounted) {
    const String path = captionPath(status);
    const uint16_t count = countLines(path);
    if (count > 0) {
      const uint16_t choice = pickDifferent(count, state.lastCaption);
      state.lastCaption = static_cast<int16_t>(choice);
      const String line = readLine(path, choice);
      if (!line.isEmpty()) return line;
    }
  }
  // No caption bank flashed: the status label is still better than an empty band.
  return String(labelFor(status, gLanguage));
}

}  // namespace content

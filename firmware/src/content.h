// Reads the LittleFS payload built by tools/build_memes.py: meme JPEGs and caption banks.
//
// The on-disk format is deliberately plain text so the firmware needs no JSON parser:
//   /memes/<orient>/<status>/index.txt   one JPEG filename per line
//   /memes/<orient>/<status>/NN.jpg      baseline JPEG sized for that orientation
//   /captions/<lang>/<status>.txt        one caption per line
// where <orient> is "land" or "port" and <lang> is "en" or "it".
#pragma once

#include <Arduino.h>

#include "status.h"

namespace content {

// Mounts LittleFS and counts what is available for the given orientation. Returns false if the
// filesystem is missing, in which case every status falls back to the built-in scene and labels.
bool begin(Orientation orientation, Language language);

bool mounted();

// Switching orientation re-counts the memes, since each orientation has its own folder.
void setOrientation(Orientation orientation);
void setLanguage(Language language);

Orientation orientation();
Language language();

// Number of memes flashed for a status; 0 means the fallback scene is used.
uint8_t memeCount(Status status);

// Total memes across all statuses, for the boot banner.
uint16_t totalMemes();

// Path of the next meme to show, or "" when the status has none. Avoids repeating the previous
// pick when there is more than one to choose from.
String nextMeme(Status status);

// A random caption for the status in the current language. Falls back to the built-in label
// if no caption bank is flashed.
String nextCaption(Status status);

}  // namespace content

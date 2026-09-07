#include "serial_link.h"

#include "say.h"

namespace serial_link {
namespace {

// One transport's half-assembled line. There are two of them -- USB and the network client --
// and they must not share a buffer: bytes from the two arrive interleaved, and gluing them
// together would produce lines neither sender ever wrote.
struct Channel {
  Stream *stream = nullptr;
  String buffer;
  uint32_t lastByteMs = 0;
  bool discarding = false;
};

Handlers gHandlers;
Stream *(*gSecondary)() = nullptr;
Channel gChannels[2];  // [0] USB, [1] network
uint32_t gLastCommandMs = 0;

// How long a half-finished line is allowed to sit before it is treated as noise. Longer than any
// gap inside a line the PC sends -- even a GIFDATA chunk arrives in one burst at 115200.
constexpr uint32_t kPartialLineMs = 250;

// Commands only the USB side may give. These carry the WiFi credentials and the shared token, so
// honouring them over the network would let whoever got in take the board off your network -- or
// read back the secret that is the only thing keeping them out.
bool isUsbOnly(const String &command) {
  return command == "WIFI" || command == "WIFIPASS" || command == "WIFIAPPLY" ||
         command == "WIFIOFF" || command == "WIFISTAT" || command == "TOKENGET";
}

void dispatch(String line, bool fromUsb) {
  line.trim();
  if (line.isEmpty()) return;

  if (line == "PING") {
    say::println(F("PONG"));
    gLastCommandMs = millis();
    return;
  }

  const int colon = line.indexOf(':');
  const String command = (colon < 0) ? line : line.substring(0, colon);
  const String value = (colon < 0) ? String() : line.substring(colon + 1);

  if (!fromUsb && isUsbOnly(command)) {
    say::printf("LOG:%s is accepted over USB only\n", command.c_str());
    return;
  }

  if (command == "STATUS") {
    Status status;
    if (!statusFromToken(value, &status)) {
      say::printf("LOG:unknown status token '%s'\n", value.c_str());
      return;
    }
    // Only a recognised STATUS feeds the watchdog: that is the PC's heartbeat, and treating any
    // stray byte as liveness would hide a half-dead sender.
    gLastCommandMs = millis();
    if (gHandlers.onStatus) gHandlers.onStatus(status);
  } else if (command == "NEXT") {
    gLastCommandMs = millis();
    if (gHandlers.onNext) gHandlers.onNext();
  } else if (command == "BRIGHT") {
    gLastCommandMs = millis();
    if (gHandlers.onBrightness) gHandlers.onBrightness(constrain(value.toInt(), 0, 100));
  } else if (command == "ROTATE") {
    gLastCommandMs = millis();
    if (gHandlers.onRotate) gHandlers.onRotate(constrain(value.toInt(), 0, 3600));
  } else if (command == "TIME") {
    gLastCommandMs = millis();
    if (gHandlers.onTime) gHandlers.onTime(value);
  } else if (command == "LANG") {
    Language language;
    if (!languageFromToken(value, &language)) {
      say::printf("LOG:unknown language '%s'\n", value.c_str());
      return;
    }
    gLastCommandMs = millis();
    if (gHandlers.onLanguage) gHandlers.onLanguage(language);
  } else if (command == "ORIENT") {
    Orientation orientation;
    if (!orientationFromToken(value, &orientation)) {
      say::printf("LOG:unknown orientation '%s'\n", value.c_str());
      return;
    }
    gLastCommandMs = millis();
    if (gHandlers.onOrientation) gHandlers.onOrientation(orientation);
  } else if (command == "MODE") {
    DisplayMode displayMode;
    if (!displayModeFromToken(value, &displayMode)) {
      say::printf("LOG:unknown mode '%s'\n", value.c_str());
      return;
    }
    gLastCommandMs = millis();
    if (gHandlers.onDisplayMode) gHandlers.onDisplayMode(displayMode);
  } else if (command == "TRANSITION") {
    gLastCommandMs = millis();
    if (gHandlers.onTransition) gHandlers.onTransition(constrain(value.toInt(), 0, 2000));
  } else if (command == "TONE") {
    Tone tone;
    if (!toneFromToken(value, &tone)) {
      say::printf("LOG:unknown tone '%s'\n", value.c_str());
      return;
    }
    gLastCommandMs = millis();
    if (gHandlers.onTone) gHandlers.onTone(tone);
  } else if (command == "CAPTION") {
    gLastCommandMs = millis();
    if (gHandlers.onCaption) gHandlers.onCaption(value);
  } else if (command == "ALERT") {
    gLastCommandMs = millis();
    // Clamped rather than rejected: a bad value should still show *something* briefly instead of
    // parking the GIF on screen forever.
    if (gHandlers.onAlert) gHandlers.onAlert(constrain(value.toInt(), 200, 60000));
  } else if (command == "GIFBEGIN") {
    gLastCommandMs = millis();
    // "<bytes>:<crc32>". Both unsigned 32-bit, so they are parsed as 64-bit and narrowed --
    // toInt() is signed and a CRC above 2^31 would come back negative.
    const int split = value.indexOf(':');
    if (split < 0) {
      say::println(F("EVT:GIFERR:malformed GIFBEGIN"));
      return;
    }
    if (gHandlers.onGifBegin) {
      gHandlers.onGifBegin(strtoul(value.substring(0, split).c_str(), nullptr, 10),
                           strtoul(value.substring(split + 1).c_str(), nullptr, 10));
    }
  } else if (command == "GIFDATA") {
    gLastCommandMs = millis();
    if (gHandlers.onGifData) gHandlers.onGifData(value);
  } else if (command == "GIFEND") {
    gLastCommandMs = millis();
    if (gHandlers.onGifEnd) gHandlers.onGifEnd();
  } else if (command == "WIFI") {
    gLastCommandMs = millis();
    // The value is the rest of the line, so an SSID containing a colon survives.
    if (gHandlers.onWifiSsid) gHandlers.onWifiSsid(value);
  } else if (command == "WIFIPASS") {
    gLastCommandMs = millis();
    // Base64, so a password with leading or trailing spaces is not eaten by the trim() above.
    if (gHandlers.onWifiPassword) gHandlers.onWifiPassword(value);
  } else if (command == "WIFIAPPLY") {
    gLastCommandMs = millis();
    if (gHandlers.onWifiApply) gHandlers.onWifiApply();
  } else if (command == "WIFIOFF") {
    gLastCommandMs = millis();
    if (gHandlers.onWifiOff) gHandlers.onWifiOff();
  } else if (command == "WIFISTAT") {
    gLastCommandMs = millis();
    if (gHandlers.onWifiStatus) gHandlers.onWifiStatus();
  } else if (command == "TOKENGET") {
    gLastCommandMs = millis();
    if (gHandlers.onTokenGet) gHandlers.onTokenGet();
  } else {
    say::printf("LOG:ignoring '%s'\n", command.c_str());
  }
}

void pump(Channel &channel, bool fromUsb) {
  Stream *stream = channel.stream;
  if (stream == nullptr) return;

  // A partial line that stopped arriving is junk: a burst of noise from the USB bridge as the PC
  // boots or opens the port, or a sender that died mid-line. Dropping it matters because the next
  // real command would otherwise be glued onto it and parse as nothing -- which is how a board
  // plugged in at boot could stay unreachable until it was power-cycled.
  // Only with nothing waiting to be read: rendering a frame can hold up the loop for longer than
  // this timeout, and the rest of a perfectly good line would be sitting in the FIFO meanwhile.
  if (!stream->available() && (channel.buffer.length() > 0 || channel.discarding) &&
      millis() - channel.lastByteMs > kPartialLineMs) {
    channel.buffer = "";
    channel.discarding = false;
  }
  while (stream->available()) {
    channel.lastByteMs = millis();
    const char c = static_cast<char>(stream->read());
    if (c == '\n') {
      if (!channel.discarding) dispatch(channel.buffer, fromUsb);
      channel.buffer = "";
      channel.discarding = false;
    } else if (c != '\r' && !channel.discarding) {
      if (channel.buffer.length() < kMaxLine) {
        channel.buffer += c;
      } else {
        // Overlong line: discard the rest of it rather than truncating into something that
        // happens to parse as a valid command.
        channel.buffer = "";
        channel.discarding = true;
      }
    }
  }
}

}  // namespace

void begin(const Handlers &handlers) {
  gHandlers = handlers;
  gChannels[0].stream = &Serial;
  for (Channel &channel : gChannels) {
    channel.buffer.reserve(kMaxLine);
    channel.lastByteMs = millis();
  }
  gLastCommandMs = millis();
}

void setSecondary(Stream *(*provider)()) { gSecondary = provider; }

void poll() {
  Stream *network = gSecondary == nullptr ? nullptr : gSecondary();
  if (network != gChannels[1].stream) {
    // A client came or went. Whatever half a line it left behind belongs to a conversation that
    // is over, and the next one must not inherit it.
    gChannels[1].stream = network;
    gChannels[1].buffer = "";
    gChannels[1].discarding = false;
    gChannels[1].lastByteMs = millis();
  }
  pump(gChannels[0], true);
  pump(gChannels[1], false);
}

uint32_t lastCommandMs() { return gLastCommandMs; }

}  // namespace serial_link

#include "net_link.h"

#include <Preferences.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <esp_random.h>
#include <mbedtls/base64.h>

#include "say.h"
#include "serial_link.h"

namespace net_link {
namespace {

// Its own NVS namespace rather than main.cpp's "teamsmeme": nothing outside this file reads these
// keys, and a WIFIOFF that cleared the wrong one would take the display settings with it.
constexpr char kNamespace[] = "teamsnet";
constexpr char kHostname[] = "teams-status";

//: How long to let one connection attempt run before starting it again. A wrong password never
//: reports itself as such -- the association simply never completes -- so this is also what turns
//: a typo into a visible EVT:WIFI:failed rather than a silent wait.
constexpr uint32_t kConnectTimeoutMs = 20000;
constexpr uint32_t kRetryMs = 15000;
//: A client that connects and then says nothing is not ours. Dropping it keeps the single client
//: slot from being held open by a port scanner.
constexpr uint32_t kAuthTimeoutMs = 5000;
constexpr uint32_t kBeaconMs = 3000;
//: WPA2 allows 63 characters, and the token is 16 hex digits.
constexpr size_t kMaxPassword = 64;

enum class State { Off, Connecting, Waiting, Online };

Preferences gPrefs;
WiFiServer gServer(kPort);
WiFiUDP gUdp;
WiFiClient gClient;
Callbacks gCallbacks;

const char *gVersion = "";
State gState = State::Off;
String gToken;
String gStagedSsid;
String gStagedPassword;
bool gHasStagedPassword = false;

uint32_t gStateSinceMs = 0;
uint32_t gRetryAtMs = 0;
uint32_t gLastBeaconMs = 0;

bool gAuthenticated = false;
String gAuthLine;
uint32_t gClientSinceMs = 0;

void info(const String &text) {
  if (gCallbacks.onInfo != nullptr) gCallbacks.onInfo(text);
}

// 16 hex digits from the hardware RNG. Not a password -- it is what stops the neighbour whose
// laptop is on the same WiFi from putting their own captions and GIFs on your screen.
String makeToken() {
  char buffer[17];
  snprintf(buffer, sizeof(buffer), "%08x%08x", esp_random(), esp_random());
  return String(buffer);
}

void dropClient(const char *why) {
  if (gClient) {
    if (why != nullptr) say::printf("LOG:network client dropped (%s)\n", why);
    gClient.stop();
  }
  gAuthenticated = false;
  gAuthLine = "";
}

void startRadio() {
  const String ssid = gPrefs.getString("ssid", "");
  if (ssid.isEmpty()) {
    gState = State::Off;
    info("");
    reportStatus();
    return;
  }
  const String password = gPrefs.getString("pass", "");
  // persistent(false): the credentials are ours to keep in NVS, and letting the WiFi library
  // write its own copy on every begin() would be a flash write per reconnect.
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(kHostname);
  // Modem sleep. Worth roughly 30-40 mA on a board that is meant to run off a battery, at the
  // cost of a little latency on the first packet after an idle stretch -- which for a status
  // display that updates every few seconds is nothing.
  WiFi.setSleep(true);
  WiFi.begin(ssid.c_str(), password.c_str());
  gState = State::Connecting;
  gStateSinceMs = millis();
  say::printf("LOG:wifi connecting to %s\n", ssid.c_str());
  reportStatus();
}

void stopRadio() {
  dropClient(nullptr);
  gServer.end();
  gUdp.stop();
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  gState = State::Off;
  info("");
}

void onConnected() {
  gState = State::Online;
  gStateSinceMs = millis();
  gServer.begin();
  gServer.setNoDelay(true);
  gUdp.begin(0);  // any local port; the beacon only ever goes out
  gLastBeaconMs = 0;
  const String ip = WiFi.localIP().toString();
  say::printf("LOG:wifi online, %s port %u\n", ip.c_str(), kPort);
  reportStatus();
  // The board is the only thing that knows this address until somebody connects, and it may well
  // be sitting on a battery across the room. So it says so on its own screen.
  info("WiFi " + ip);
}

void onLost(const char *reason) {
  gState = State::Waiting;
  gStateSinceMs = millis();
  gRetryAtMs = millis() + kRetryMs;
  dropClient(nullptr);
  gServer.end();
  gUdp.stop();
  say::printf("EVT:WIFI:failed:%s\n", reason);
  info("");
}

void acceptClients() {
  WiFiClient incoming = gServer.available();
  if (incoming) {
    // The newcomer wins. A socket the PC has forgotten about -- a laptop that slept, a cable
    // pulled from the router -- can look connected from this end for minutes, and refusing the
    // replacement would lock the board out for exactly that long.
    if (gClient) dropClient("replaced");
    gClient = incoming;
    gClient.setNoDelay(true);
    gAuthenticated = false;
    gAuthLine = "";
    gClientSinceMs = millis();
  }

  if (!gClient) return;
  if (!gClient.connected()) {
    dropClient(nullptr);
    return;
  }
  if (gAuthenticated) return;

  // Pre-auth bytes are read here rather than by serial_link, so an unauthenticated client's line
  // can never reach dispatch() at all.
  while (gClient.available()) {
    const char c = static_cast<char>(gClient.read());
    if (c == '\r') continue;
    if (c != '\n') {
      if (gAuthLine.length() < serial_link::kMaxLine) gAuthLine += c;
      continue;
    }
    gAuthLine.trim();
    const bool ok = gAuthLine.startsWith("AUTH:") && gAuthLine.substring(5) == gToken;
    gAuthLine = "";
    if (!ok) {
      // Straight at the socket, not through say::, so a bad token learns nothing about the board
      // and the answer does not clutter the USB log with somebody else's port scan.
      gClient.println(F("LOG:bad token"));
      dropClient("bad token");
      return;
    }
    gAuthenticated = true;
    say::printf("LOG:network client %s\n", gClient.remoteIP().toString().c_str());
    // Same greeting the USB side gets on boot: it is what pc_app/net_link.py waits for, and by
    // now client() returns the socket so say:: reaches it.
    say::printf("READY:%s\n", gVersion);
    return;
  }

  if (millis() - gClientSinceMs > kAuthTimeoutMs) dropClient("no token");
}

void beacon() {
  if (millis() - gLastBeaconMs < kBeaconMs) return;
  gLastBeaconMs = millis();
  // Subnet broadcast rather than 255.255.255.255, which plenty of routers and firewalls drop.
  // The token is deliberately not in here: this says where the board is, not how to drive it.
  if (gUdp.beginPacket(WiFi.broadcastIP(), kBeaconPort) != 1) return;
  gUdp.printf("TEAMSMEME:%s:%s:%u\n", gVersion, WiFi.localIP().toString().c_str(), kPort);
  gUdp.endPacket();
}

}  // namespace

void begin(const char *version, const Callbacks &callbacks) {
  gVersion = version;
  gCallbacks = callbacks;
  gPrefs.begin(kNamespace, false);

  gToken = gPrefs.getString("token", "");
  if (gToken.isEmpty()) {
    gToken = makeToken();
    gPrefs.putString("token", gToken);
  }

  // Both output and input go through the same provider, so a client that drops takes its channel
  // with it in one place.
  say::setSecondary(client);
  serial_link::setSecondary(client);

  startRadio();
}

void tick() {
  switch (gState) {
    case State::Off:
      return;

    case State::Connecting:
      if (WiFi.status() == WL_CONNECTED) {
        onConnected();
      } else if (millis() - gStateSinceMs > kConnectTimeoutMs) {
        // A wrong password looks exactly like a router that is out of range, so the reason can
        // only ever be this vague.
        gState = State::Waiting;
        gStateSinceMs = millis();
        gRetryAtMs = millis() + kRetryMs;
        WiFi.disconnect();
        say::println(F("EVT:WIFI:failed:no answer -- wrong password, or out of range"));
      }
      return;

    case State::Waiting:
      if (static_cast<int32_t>(millis() - gRetryAtMs) >= 0) startRadio();
      return;

    case State::Online:
      if (WiFi.status() != WL_CONNECTED) {
        onLost("lost");
        return;
      }
      acceptClients();
      beacon();
      return;
  }
}

Stream *client() {
  if (gState != State::Online || !gAuthenticated || !gClient || !gClient.connected()) {
    return nullptr;
  }
  return &gClient;
}

void stageSsid(const String &ssid) {
  gStagedSsid = ssid;
  say::printf("LOG:wifi ssid staged (%u chars)\n", static_cast<unsigned>(gStagedSsid.length()));
}

void stagePassword(const String &base64) {
  uint8_t raw[kMaxPassword + 1];
  size_t decoded = 0;
  const int rc = mbedtls_base64_decode(raw, kMaxPassword, &decoded,
                                       reinterpret_cast<const uint8_t *>(base64.c_str()),
                                       base64.length());
  if (rc != 0) {
    say::println(F("EVT:WIFI:failed:bad password encoding"));
    return;
  }
  raw[decoded] = '\0';
  gStagedPassword = String(reinterpret_cast<char *>(raw));
  gHasStagedPassword = true;
  say::printf("LOG:wifi password staged (%u chars)\n", static_cast<unsigned>(decoded));
}

void apply() {
  if (!gStagedSsid.isEmpty()) gPrefs.putString("ssid", gStagedSsid);
  if (gHasStagedPassword) gPrefs.putString("pass", gStagedPassword);
  gStagedSsid = "";
  gStagedPassword = "";
  gHasStagedPassword = false;
  if (gPrefs.getString("ssid", "").isEmpty()) {
    say::println(F("EVT:WIFI:failed:no ssid"));
    return;
  }
  stopRadio();
  startRadio();
}

void disable() {
  gPrefs.remove("ssid");
  gPrefs.remove("pass");
  stopRadio();
  say::println(F("LOG:wifi credentials cleared"));
  reportStatus();
}

void reportStatus() {
  switch (gState) {
    case State::Off:
      say::println(F("EVT:WIFI:off"));
      return;
    case State::Connecting:
    case State::Waiting:
      say::println(F("EVT:WIFI:connecting"));
      return;
    case State::Online:
      say::printf("EVT:WIFI:online:%s:%d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
      return;
  }
}

void reportToken() { say::printf("EVT:TOKEN:%s\n", gToken.c_str()); }

}  // namespace net_link

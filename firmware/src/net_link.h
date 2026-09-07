// The second transport: WiFi, so the board can live on a powerbank instead of a USB cable.
//
// Same line protocol as the serial link -- see docs/PROTOCOL.md. This module only carries the
// bytes: it accepts one TCP client, checks the shared token, and hands the stream to serial_link,
// which parses exactly as it does for USB. Credentials arrive over USB and live in NVS.
#pragma once

#include <Arduino.h>

namespace net_link {

//: TCP port the board listens on, and the UDP port its discovery beacon is broadcast to.
//: pc_app/net_link.py has the same two numbers.
constexpr uint16_t kPort = 3141;
constexpr uint16_t kBeaconPort = 3142;

struct Callbacks {
  //: Something worth putting on screen while no PC is driving the board -- "WiFi 192.168.1.42"
  //: once it is online, empty otherwise. Which is how you find the board when it is across the
  //: desk on a battery and the app cannot see it.
  void (*onInfo)(const String &text) = nullptr;
};

//: Starts the radio if credentials are stored, and does nothing at all if they are not -- a board
//: that has never been provisioned behaves exactly as it did before WiFi existed.
void begin(const char *version, const Callbacks &callbacks);

//: Drives connection, the listening socket and the beacon. Called every loop, and never blocks:
//: the mascot animates at ~25 fps and alert::tick() decodes a GIF frame at a time.
void tick();

//: The authenticated client, or nullptr. Registered with say:: and serial_link:: so neither of
//: them needs to know what a socket is.
Stream *client();

// -- provisioning, all of it USB-only (see isUsbOnly in serial_link.cpp) ---------------------

void stageSsid(const String &ssid);
void stagePassword(const String &base64);
void apply();
void disable();
void reportStatus();
void reportToken();

}  // namespace net_link

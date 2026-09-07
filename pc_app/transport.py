"""Which way to reach the board -- the cable, the network, or whichever answers.

`SerialLink` and `NetworkLink` present the same surface (`send`, `read_lines`, `ensure_connected`,
`close`, `force_rescan`, `connected`, `port`, `last_error`), so `Worker` never learns which one it
is holding. That is the whole point of this module: everything above it is transport-blind.
"""

from __future__ import annotations

import logging

from pc_app.net_link import NetworkLink
from pc_app.serial_link import SerialLink

log = logging.getLogger(__name__)

#: config.transport. "auto" is the default and covers the ordinary case: a board that is usually
#: on WiFi but sometimes plugged in to be provisioned or reflashed.
TRANSPORTS = ("auto", "serial", "network")


class AutoLink:
    """Whichever transport is answering, USB first.

    USB wins ties because it is the one that cannot be wrong about which board it reached, and
    because plugging the cable in is what you do when the wireless side has gone wrong -- so it
    had better take over when you do.
    """

    def __init__(self, serial: SerialLink, network: NetworkLink):
        self.serial = serial
        self.network = network
        self.dry_run = serial.dry_run
        self._active: SerialLink | NetworkLink | None = None

    @property
    def kind(self) -> str | None:
        """Which transport is carrying us right now, or None while nothing is."""
        return self._active.kind if self._active is not None else None

    @property
    def connected(self) -> bool:
        return self._active is not None and self._active.connected

    @property
    def port(self) -> str | None:
        return self._active.port if self._active is not None else None

    @property
    def last_error(self) -> str | None:
        if self._active is not None:
            return self._active.last_error
        # Neither worked, so neither reason alone is the answer.
        reasons = [link.last_error for link in (self.serial, self.network) if link.last_error]
        return " / ".join(reasons) if reasons else None

    def ensure_connected(self) -> bool:
        if self._active is self.network and self.serial.ensure_connected():
            # The cable is back, so it takes over. It is the only way to change the board's WiFi
            # or read its token, and plugging it in is what you do when the wireless side has
            # gone wrong -- so it had better be what you end up talking over.
            log.info("the cable is back; taking the board off %s", self.network.port)
            self.network.close()
            self._active = self.serial
            return True
        if self._active is not None:
            if self._active.ensure_connected():
                return True
            # It dropped. The other one may well be there -- the cable coming out is exactly when
            # WiFi should take over -- so fall through rather than sitting on a dead link.
            self._active = None
        for candidate in (self.serial, self.network):
            if candidate.ensure_connected():
                self._active = candidate
                log.info("talking to the board on %s", candidate.port or "a link with no name")
                return True
        return False

    def send(self, line: str) -> bool:
        if not self.ensure_connected():
            return False
        return self._active.send(line)

    def read_lines(self):
        if self._active is None:
            return iter(())
        return self._active.read_lines()

    def close(self) -> None:
        self._active = None
        self.serial.close()
        self.network.close()

    def force_rescan(self) -> None:
        self._active = None
        self.serial.force_rescan()
        self.network.force_rescan()

    def configure(self, host: str | None = None, token: str | None = None) -> None:
        """Pass an address or token learned over USB to the side that needs it."""
        self.network.configure(host=host, token=token)


def make_link(config, dry_run: bool = False):
    """The link Worker will drive, built from what the config asks for."""
    if dry_run:
        # Nothing is opened either way, and SerialLink already knows how to print instead of send.
        return SerialLink(port=config.port, baud=config.baud, dry_run=True)

    choice = (config.transport or "auto").lower()
    if choice not in TRANSPORTS:
        log.warning("unknown transport %r; using auto", config.transport)
        choice = "auto"

    if choice == "serial":
        return SerialLink(port=config.port, baud=config.baud)

    network = NetworkLink(
        host=config.board_host,
        tcp_port=config.board_port,
        token=config.board_token,
        discovery_port=config.discovery_port,
    )
    if choice == "network":
        return network
    return AutoLink(SerialLink(port=config.port, baud=config.baud), network)


def adopt(link, host: str | None = None, token: str | None = None) -> None:
    """Tell the network side about an address or token the board just reported over USB.

    A plain SerialLink has nothing to do with either, so it simply has no configure() to call.
    """
    configure = getattr(link, "configure", None)
    if configure is not None:
        configure(host=host, token=token)

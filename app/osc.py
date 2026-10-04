"""OSC bridge: send avatar changes to VRChat and receive avatar-change events."""

from __future__ import annotations

import threading
import time
from typing import Callable

from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer
from pythonosc.udp_client import SimpleUDPClient

AVATAR_CHANGE_ADDRESS = "/avatar/change"


class OSCBridge:
    """Owns the UDP socket pair used to talk to VRChat over OSC."""

    def __init__(
        self,
        send_ip: str = "127.0.0.1",
        send_port: int = 9000,
        receive_port: int = 9001,
    ) -> None:
        self.send_ip = send_ip
        self.send_port = send_port
        self.receive_port = receive_port

        self._client: SimpleUDPClient | None = None
        self._server: ThreadingOSCUDPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        self.current_avatar_id: str | None = None
        self.last_message_time: float = 0.0
        self.listening = False
        self.error: str | None = None
        self._on_avatar_change: list[Callable[[str], None]] = []

    # ------------------------------------------------------------------ events
    def add_avatar_change_listener(self, callback: Callable[[str], None]) -> None:
        self._on_avatar_change.append(callback)

    def _handle_avatar_change(self, address: str, *args) -> None:
        if not args:
            return
        avatar_id = str(args[0])
        with self._lock:
            self.current_avatar_id = avatar_id
            self.last_message_time = time.time()
        for cb in self._on_avatar_change:
            try:
                cb(avatar_id)
            except Exception:
                pass

    def _handle_other(self, address: str, *args) -> None:
        with self._lock:
            self.last_message_time = time.time()

    # ------------------------------------------------------------------- start
    def start(self) -> None:
        if self.listening:
            return
        try:
            self._client = SimpleUDPClient(self.send_ip, self.send_port)
        except Exception as exc:  # pragma: no cover - defensive
            self.error = f"Could not create OSC sender: {exc}"
            return

        dispatcher = Dispatcher()
        dispatcher.map(AVATAR_CHANGE_ADDRESS, self._handle_avatar_change)
        dispatcher.set_default_handler(self._handle_other)
        try:
            self._server = ThreadingOSCUDPServer(("127.0.0.1", self.receive_port), dispatcher)
        except OSError as exc:
            self.error = (
                f"Could not listen on UDP port {self.receive_port}: {exc}. "
                "Is another app using it?"
            )
            self.listening = False
            return

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.listening = True
        self.error = None

    def stop(self) -> None:
        self.listening = False
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
        self._thread = None
        self._client = None

    # ------------------------------------------------------------------- send
    def change_avatar(self, avatar_id: str) -> bool:
        if self._client is None:
            return False
        try:
            self._client.send_message(AVATAR_CHANGE_ADDRESS, avatar_id)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------- info
    def seen_traffic(self) -> bool:
        with self._lock:
            return (time.time() - self.last_message_time) < 10.0

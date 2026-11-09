#!/usr/bin/env python3
"""Telegram bot that toggles a WireGuard client and drops it if the server dies.

Only the standard library is used. Configuration is a JSON file passed as the
first argument; the bot token is read from the file named in it.
"""

import json
import logging
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("knock-knock")

KEYBOARD = {
    "keyboard": [[{"text": "/on"}, {"text": "/off"}, {"text": "/status"}]],
    "resize_keyboard": True,
}


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, params=None, timeout=10):
        data = json.dumps(params or {}).encode()
        req = urllib.request.Request(
            self.base + method,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.load(resp)
        if not body.get("ok"):
            raise RuntimeError(f"{method}: {body.get('description')}")
        return body["result"]

    def send(self, chat_id, text):
        self.call(
            "sendMessage",
            {"chat_id": chat_id, "text": text, "reply_markup": KEYBOARD},
        )


class Tunnel:
    """Thin wrapper over the systemd unit that brings the interface up."""

    def __init__(self, cfg):
        self.unit = cfg["unit"]
        self.interface = cfg["interface"]
        self.check_host = cfg.get("checkHost")
        self.ping_timeout = cfg["pingTimeout"]
        self.handshake_max_age = cfg["handshakeMaxAge"]

    def _systemctl(self, *args):
        return subprocess.run(
            ["systemctl", *args, self.unit],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def is_active(self):
        return self._systemctl("is-active", "--quiet").returncode == 0

    def start(self):
        res = self._systemctl("start")
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or f"systemctl start {self.unit} failed")

    def stop(self):
        res = self._systemctl("stop")
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or f"systemctl stop {self.unit} failed")

    def latest_handshake(self):
        res = subprocess.run(
            ["wg", "show", self.interface, "latest-handshakes"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode != 0:
            return 0
        stamps = [int(line.split()[1]) for line in res.stdout.splitlines() if line.strip()]
        return max(stamps, default=0)

    def healthy(self):
        """True if the server answers through the tunnel."""
        if self.check_host:
            res = subprocess.run(
                [
                    "ping", "-c", "1", "-W", str(self.ping_timeout),
                    "-I", self.interface, self.check_host,
                ],
                capture_output=True,
                timeout=self.ping_timeout + 5,
            )
            return res.returncode == 0
        handshake = self.latest_handshake()
        return handshake > 0 and time.time() - handshake <= self.handshake_max_age


def ping_host(host, timeout):
    res = subprocess.run(
        ["ping", "-c", "1", "-W", str(timeout), host],
        capture_output=True,
        timeout=timeout + 5,
    )
    return res.returncode == 0


class Bot:
    def __init__(self, cfg):
        with open(cfg["tokenFile"]) as f:
            token = f.read().strip()
        self.tg = Telegram(token)
        self.tunnel = Tunnel(cfg)
        self.allowed = set(cfg["allowedChatIds"])
        self.check_interval = cfg["checkInterval"]
        self.down_timeout = cfg["downTimeout"]
        self.pre_check_host = cfg.get("preCheckHost")
        self.ping_timeout = cfg["pingTimeout"]

        self.lock = threading.Lock()
        # Watchdog state; armed whenever the unit is active.
        self.armed = False
        self.confirmed = False
        self.last_ok = 0.0
        # Messages are queued so they survive a period without connectivity.
        self.outbox = queue.Queue()

    # --- notifications -------------------------------------------------------

    def notify(self, text, chat_id=None):
        targets = [chat_id] if chat_id is not None else sorted(self.allowed)
        for target in targets:
            self.outbox.put((target, text))

    def sender_loop(self):
        while True:
            chat_id, text = self.outbox.get()
            while True:
                try:
                    self.tg.send(chat_id, text)
                    break
                except Exception as e:
                    log.warning("sendMessage failed, retrying: %s", e)
                    time.sleep(5)

    # --- watchdog ------------------------------------------------------------

    def arm(self):
        self.armed = True
        self.confirmed = False
        self.last_ok = time.monotonic()

    def watchdog_loop(self):
        while True:
            try:
                self.watchdog_tick()
            except Exception:
                log.exception("watchdog tick failed")
            time.sleep(self.check_interval)

    def watchdog_tick(self):
        with self.lock:
            if not self.tunnel.is_active():
                if self.armed:
                    self.armed = False
                    self.notify("WireGuard выключен.")
                return
            if not self.armed:
                # Started by someone else or the bot was restarted.
                log.info("unit %s is active, arming watchdog", self.tunnel.unit)
                self.arm()

        ok = self.tunnel.healthy()
        now = time.monotonic()

        with self.lock:
            if not self.armed:
                return
            if ok:
                self.last_ok = now
                if not self.confirmed:
                    self.confirmed = True
                    self.notify("WireGuard включён, сервер отвечает.")
                return
            silent = now - self.last_ok
            log.info("server is not answering for %.0fs", silent)
            if silent < self.down_timeout:
                return
            self.armed = False

        log.warning("server silent for %.0fs, stopping %s", silent, self.tunnel.unit)
        try:
            self.tunnel.stop()
        except Exception as e:
            log.error("failed to stop tunnel: %s", e)
            self.notify(f"Сервер не отвечает, но остановить WireGuard не удалось: {e}")
            return
        self.notify(f"Сервер не отвечал {silent:.0f} с, WireGuard отключён.")

    # --- commands ------------------------------------------------------------

    def cmd_on(self, chat_id):
        if self.tunnel.is_active():
            self.notify("WireGuard уже включён.", chat_id)
            return
        if self.pre_check_host and not ping_host(self.pre_check_host, self.ping_timeout):
            self.notify(f"{self.pre_check_host} не отвечает на ping, не включаю.", chat_id)
            return
        # The lock keeps the watchdog from seeing a half-started unit.
        with self.lock:
            self.arm()
            try:
                self.tunnel.start()
            except Exception as e:
                self.armed = False
                self.notify(f"Не удалось включить: {e}", chat_id)
                return
        self.notify(
            f"Включаю. Если сервер не ответит за {self.down_timeout} с, отключу.",
            chat_id,
        )

    def cmd_off(self, chat_id):
        with self.lock:
            self.armed = False
            try:
                self.tunnel.stop()
            except Exception as e:
                self.notify(f"Не удалось выключить: {e}", chat_id)
                return
        self.notify("WireGuard выключен.", chat_id)

    def cmd_status(self, chat_id):
        if not self.tunnel.is_active():
            self.notify("WireGuard выключен.", chat_id)
            return
        with self.lock:
            ago = time.monotonic() - self.last_ok
            confirmed = self.confirmed
        state = "сервер отвечал" if confirmed else "ждём первого ответа сервера,"
        self.notify(f"WireGuard включён, {state} {ago:.0f} с назад.", chat_id)

    def cmd_help(self, chat_id):
        self.notify(
            "/on — включить WireGuard\n"
            "/off — выключить\n"
            "/status — состояние",
            chat_id,
        )

    COMMANDS = {
        "/on": cmd_on,
        "/off": cmd_off,
        "/status": cmd_status,
        "/start": cmd_help,
        "/help": cmd_help,
    }

    def handle(self, update):
        msg = update.get("message")
        if not msg or "text" not in msg:
            return
        chat_id = msg["chat"]["id"]
        user_id = msg.get("from", {}).get("id")
        if chat_id not in self.allowed and user_id not in self.allowed:
            log.warning("ignoring message from chat %s user %s", chat_id, user_id)
            return
        command = msg["text"].split()[0].split("@")[0].lower() if msg["text"].strip() else ""
        handler = self.COMMANDS.get(command)
        if handler is None:
            self.cmd_help(chat_id)
            return
        log.info("chat %s: %s", chat_id, command)
        handler(self, chat_id)

    def poll_loop(self):
        offset = None
        while True:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            try:
                updates = self.tg.call("getUpdates", params, timeout=40)
            except (urllib.error.URLError, OSError, RuntimeError) as e:
                log.warning("getUpdates failed: %s", e)
                time.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    self.handle(update)
                except Exception:
                    log.exception("failed to handle update")

    def run(self):
        for target in (self.sender_loop, self.watchdog_loop):
            threading.Thread(target=target, daemon=True).start()
        self.poll_loop()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )
    with open(sys.argv[1]) as f:
        cfg = json.load(f)
    Bot(cfg).run()


if __name__ == "__main__":
    main()

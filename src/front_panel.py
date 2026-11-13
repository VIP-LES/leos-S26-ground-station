"""Front panel for the ground station: a physical button that sends the cutdown, and LEDs that show status.

Runs inside the receiver process (index.py), on the Pi itself, with no network, no SSH and no
hotspot. Pins come from the ground-station schematic (BCM numbers):
    nBTN -> GPIO12 (pressed = low)    RED_LED -> GPIO16    YELLOW_LED -> GPIO20    GREEN_LED -> GPIO21

How to fire (three deliberate steps, so a bump or a pocket press does nothing):
    1. HOLD the button for 3 s              -> yellow turns solid, then keeps flashing: ARMED
    2. RELEASE it                           -> you have 10 s
    3. HOLD it again for 2 s                -> the cutdown packet is queued and transmitted
Releasing early, or letting the 10 s run out, disarms it again.

LEDs:
    green, brief flash every 2 s    the receiver is running and hearing the payload (last 15 s)
    (nothing)                       not hearing the payload, or the receiver is not running
    red and yellow taking turns     the radio is not working (cannot open or failed); the button will not fire
    red, slow blink                 configuration problem (no key file or no callsign): the button will not fire
    yellow solid                    button held, arming
    yellow, fast flash              ARMED, release and press again to fire
    yellow solid + red flash        firing, keep holding
    red solid                       transmitting
    red + yellow + green, all on    TRANSMITTED (10 s). The radio gets no reply, so this means "sent", not "payload cut down"
    red, fast flash (10 s)          the send failed

Needs FRONT_PANEL_ENABLED=true, RADIO_TX_ENABLED=true, GROUND_CALLSIGN, and a key file (CUTDOWN_KEY_FILE,
default ~/leos-state/cutdown_key, readable only by this user). Bring-up on the real hardware:
    python3 -m src.front_panel --test        LED cycle and live button readout
    python3 -m src.front_panel --practice    the full button and LED sequence with a PRETEND send
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

import cutdown_command

logger = logging.getLogger(__name__)

ARM_HOLD_S = 3.0        # hold this long to arm
FIRE_HOLD_S = 2.0       # hold this long, after arming, to fire
ARM_WINDOW_S = 10.0     # time allowed between releasing and pressing again
RESULT_S = 10.0         # how long the sent / failed pattern stays on
LOCKOUT_S = 3.0         # button ignored after a result
DEBOUNCE_S = 0.05
LINK_FRESH_S = 15.0     # green only if a packet arrived this recently
STEP_S = 0.02


class ButtonLogic:
    """The button state machine. Pure: feed it the time and whether the button is down."""

    def __init__(self, arm_hold_s=ARM_HOLD_S, fire_hold_s=FIRE_HOLD_S, arm_window_s=ARM_WINDOW_S,
                 result_s=RESULT_S, lockout_s=LOCKOUT_S):
        self.arm_hold_s, self.fire_hold_s = arm_hold_s, fire_hold_s
        self.arm_window_s, self.result_s, self.lockout_s = arm_window_s, result_s, lockout_s
        self.state = "wait_release"     # a button already held at boot never arms anything
        self._t0 = 0.0
        self._until = 0.0

    def update(self, now: float, pressed: bool) -> str | None:
        """Advance the machine. Returns "fire" exactly once, when the second hold completes."""
        s = self.state
        if s == "wait_release":
            if not pressed:
                self.state = "idle"
        elif s == "idle":
            if pressed:
                self.state, self._t0 = "arming", now
        elif s == "arming":
            if not pressed:
                self.state = "idle"                      # released too soon: nothing happens
            elif now - self._t0 >= self.arm_hold_s:
                self.state = "armed_pressed"             # armed, but still holding
        elif s == "armed_pressed":
            if not pressed:
                self.state, self._until = "armed", now + self.arm_window_s
        elif s == "armed":
            if now >= self._until:
                self.state = "idle"                      # window ran out: disarmed
            elif pressed:
                self.state, self._t0 = "firing", now
        elif s == "firing":
            if not pressed:
                self.state = "armed"                     # let go early: still armed until the window ends
            elif now - self._t0 >= self.fire_hold_s:
                self.state = "sending"
                return "fire"
        elif s in ("result_ok", "result_fail"):
            if now >= self._until:
                self.state, self._until = "lockout", now + self.lockout_s
        elif s == "lockout":
            if now >= self._until and not pressed:       # needs a fresh press afterwards
                self.state = "idle"
        return None                                      # "sending" waits for finish()

    def finish(self, now: float, ok: bool) -> None:
        """Call once the send has succeeded or failed."""
        self.state = "result_ok" if ok else "result_fail"
        self._until = now + self.result_s


def led_levels(state: str, now: float, link_ok: bool, config_ok: bool, radio_ok: bool = True) -> dict[str, bool]:
    """Which LEDs are on, for a given state and time."""
    fast = int(now * 5) % 2 == 0          # about 2.5 flashes a second
    leds = {"red": False, "yellow": False, "green": False}
    if state in ("wait_release", "idle", "lockout"):
        if not radio_ok:
            alternate = (now % 2.0) < 1.0           # red and yellow take turns: the radio is not working
            leds["red"], leds["yellow"] = alternate, not alternate
        elif not config_ok:
            leds["red"] = (now % 2.0) < 1.0
        elif link_ok:
            leds["green"] = (now % 2.0) < 0.1
    elif state in ("arming", "armed_pressed"):
        leds["yellow"] = True
    elif state == "armed":
        leds["yellow"] = fast
    elif state == "firing":
        leds["yellow"], leds["red"] = True, fast
    elif state == "sending":
        leds["red"] = True
    elif state == "result_ok":
        leds = {"red": True, "yellow": True, "green": True}
    elif state == "result_fail":
        leds["red"] = fast
    return leds


class LgpioPins:
    """The real pins, through lgpio (the same library the radio uses)."""

    def __init__(self, pins: dict[str, int], *, led_active_low: bool = False, button_active_low: bool = True):
        import lgpio

        self.lgpio, self.pins = lgpio, pins
        self.led_active_low, self.button_active_low = led_active_low, button_active_low
        self.h = lgpio.gpiochip_open(0)
        pull = lgpio.SET_PULL_UP if button_active_low else lgpio.SET_PULL_DOWN
        lgpio.gpio_claim_input(self.h, pins["button"], pull)
        for name in ("red", "yellow", "green"):
            lgpio.gpio_claim_output(self.h, pins[name], self._level(False))

    def _level(self, on: bool) -> int:
        return int(on != self.led_active_low)

    def read_button(self) -> bool:
        value = self.lgpio.gpio_read(self.h, self.pins["button"])
        return value == 0 if self.button_active_low else value == 1

    def write_leds(self, levels: dict[str, bool]) -> None:
        for name, on in levels.items():
            self.lgpio.gpio_write(self.h, self.pins[name], self._level(on))

    def close(self) -> None:
        try:
            self.write_leds({"red": False, "yellow": False, "green": False})
            self.lgpio.gpiochip_close(self.h)
        except Exception:
            pass


def _key_file() -> Path:
    return Path(os.environ.get("CUTDOWN_KEY_FILE") or "~/leos-state/cutdown_key").expanduser()


def load_cutdown_config() -> tuple[int, str]:
    """(authorization key, callsign) or raise ValueError saying what is missing."""
    callsign = (os.environ.get("GROUND_CALLSIGN") or "").strip()
    if not callsign:
        raise ValueError("GROUND_CALLSIGN is not set")
    raw = os.environ.get("CUTDOWN_AUTH_KEY")
    if not raw:
        path = _key_file()
        try:
            raw = path.read_text().strip()
        except OSError as exc:
            raise ValueError(f"cannot read the key file {path}: {exc.strerror}") from exc
        if path.stat().st_mode & 0o077:
            logger.warning("%s is readable by other users; run: chmod 600 %s", path, path)
    try:
        key = int(raw.strip(), 0)
    except ValueError as exc:
        raise ValueError("the cutdown key must be a number (decimal or 0x hex)") from exc
    if not 0 < key <= 0xFFFFFFFF:
        raise ValueError("the cutdown key must be a non-zero 32-bit number")
    return key, callsign


class FrontPanel:
    def __init__(self, gpio, status: dict, *, practice: bool = False, expires_s: float = 30.0,
                 logic: ButtonLogic | None = None):
        self.gpio, self.status, self.practice, self.expires_s = gpio, status, practice, expires_s
        self.logic = logic or ButtonLogic()
        self._pending = None            # (request id, spool dir, request path, give-up time)
        self._raw, self._raw_since, self._stable = False, 0.0, False
        self._last_leds: dict[str, bool] | None = None
        self._last_state = None
        self.config_ok = True
        self._check_config()

    def _check_config(self) -> None:
        if self.practice:
            self.config_ok = True
            return
        try:
            load_cutdown_config()
            self.config_ok = True
        except ValueError as exc:
            self.config_ok = False
            logger.error("Front panel cannot fire: %s", exc)
        if os.environ.get("RADIO_TX_ENABLED", "").strip().lower() != "true":
            logger.error("Front panel: RADIO_TX_ENABLED is not true, so a button press would be refused")

    def _debounced(self, now: float, raw: bool) -> bool:
        if raw != self._raw:
            self._raw, self._raw_since = raw, now
        if now - self._raw_since >= DEBOUNCE_S:
            self._stable = raw
        return self._stable

    def link_ok(self, now: float) -> bool:
        last = self.status.get("last_rx")
        return last is not None and now - last < LINK_FRESH_S

    def _start_fire(self, now: float) -> None:
        if self.practice:
            logger.warning("FRONT PANEL PRACTICE: the cutdown would be sent now (nothing is transmitted)")
            self._pending = ("practice", None, None, now + 1.5)
            return
        if not self.status.get("radio_ok", True):
            logger.error("FRONT PANEL: cutdown NOT sent: the radio is not working")
            self.logic.finish(now, False)
            return
        try:
            key, callsign = load_cutdown_config()
            data = cutdown_command.build_cutdown_packet(authorization_key=key, callsign=callsign)
            tx_dir = cutdown_command.tx_spool_dir()
            request_id, path = cutdown_command.write_request(data, tx_dir, self.expires_s)
        except Exception as exc:
            logger.error("FRONT PANEL: cutdown NOT sent: %s", exc)
            self.logic.finish(now, False)
            return
        logger.warning("FRONT PANEL: cutdown requested (%s)", cutdown_command.describe(data))
        self._pending = (request_id, tx_dir, path, now + self.expires_s + 5)

    def _poll_pending(self, now: float) -> None:
        request_id, tx_dir, path, give_up = self._pending
        if request_id == "practice":
            if now >= give_up:
                self._pending = None
                self.logic.finish(now, True)
            return
        result = cutdown_command.check_result(tx_dir, request_id)
        if result is not None:
            ok = result.get("status") == "sent"
            (logger.warning if ok else logger.error)(
                "FRONT PANEL: cutdown %s (%s)", result.get("status"), result.get("detail"))
            self._pending = None
            self.logic.finish(now, ok)
        elif now > give_up:
            path.unlink(missing_ok=True)   # withdrawn, so it can never be sent late
            logger.error("FRONT PANEL: no answer from the receiver; the request was withdrawn")
            self._pending = None
            self.logic.finish(now, False)

    def step(self, now: float) -> None:
        pressed = self._debounced(now, self.gpio.read_button())
        if self.logic.update(now, pressed) == "fire":
            self._start_fire(now)
        if self._pending is not None:
            self._poll_pending(now)
        if self.logic.state != self._last_state:
            logger.info("Front panel: %s", self.logic.state)
            self._last_state = self.logic.state
        leds = led_levels(self.logic.state, now, self.link_ok(now), self.config_ok,
                          self.status.get("radio_ok", True))
        if leds != self._last_leds:
            self.gpio.write_leds(leds)
            self._last_leds = leds

    def boot_sequence(self, sleep=time.sleep) -> None:
        """Light each LED in turn so you can see the process started and the wiring works."""
        for name in ("red", "yellow", "green"):
            self.gpio.write_leds({"red": name == "red", "yellow": name == "yellow", "green": name == "green"})
            sleep(0.3)
        self.gpio.write_leds({"red": False, "yellow": False, "green": False})

    def run(self, stop: threading.Event) -> None:
        try:
            self.boot_sequence()
            while not stop.is_set():
                self.step(time.monotonic())
                time.sleep(STEP_S)
        except Exception:
            logger.exception("Front panel stopped")
        finally:
            self.gpio.close()


def _pins_from_env() -> dict[str, int]:
    return {"button": int(os.environ.get("FRONT_PANEL_BTN_PIN", 12)),
            "red": int(os.environ.get("FRONT_PANEL_RED_PIN", 16)),
            "yellow": int(os.environ.get("FRONT_PANEL_YELLOW_PIN", 20)),
            "green": int(os.environ.get("FRONT_PANEL_GREEN_PIN", 21))}


def _gpio_from_env() -> LgpioPins:
    return LgpioPins(_pins_from_env(),
                     led_active_low=os.environ.get("FRONT_PANEL_LED_ACTIVE_LOW", "").lower() == "true",
                     button_active_low=os.environ.get("FRONT_PANEL_BTN_ACTIVE_HIGH", "").lower() != "true")


def start_front_panel(stop: threading.Event, status: dict) -> threading.Thread | None:
    """Start the front panel in a background thread if FRONT_PANEL_ENABLED=true."""
    if os.environ.get("FRONT_PANEL_ENABLED", "").strip().lower() != "true":
        return None
    panel = FrontPanel(_gpio_from_env(), status)
    thread = threading.Thread(target=panel.run, args=(stop,), daemon=True, name="front-panel")
    thread.start()
    logger.info("Front panel started (hold 3 s, release, hold 2 s to send the cutdown)")
    return thread


def _bring_up(mode: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    gpio = _gpio_from_env()
    try:
        if mode == "--test":
            print("LED test: red, yellow, green one at a time, then all three. Then 30 s of button readout.")
            for _ in range(2):
                for name in ("red", "yellow", "green"):
                    gpio.write_leds({"red": name == "red", "yellow": name == "yellow", "green": name == "green"})
                    print(f"  {name} ON")
                    time.sleep(1.0)
            gpio.write_leds({"red": True, "yellow": True, "green": True}); print("  all ON"); time.sleep(1.0)
            gpio.write_leds({"red": False, "yellow": False, "green": False})
            print("Press and release the button. It should say PRESSED while held.")
            last, end = None, time.monotonic() + 30
            while time.monotonic() < end:
                now = gpio.read_button()
                if now != last:
                    print("  button:", "PRESSED" if now else "released"); last = now
                time.sleep(0.01)
        else:
            print("PRACTICE mode: the full button and LED sequence, with a PRETEND send. Ctrl-C to stop.")
            status = {"last_rx": time.monotonic()}
            panel = FrontPanel(gpio, status, practice=True)
            panel.boot_sequence()
            try:
                while True:
                    status["last_rx"] = time.monotonic()      # pretend the payload is being heard
                    panel.step(time.monotonic())
                    time.sleep(STEP_S)
            except KeyboardInterrupt:
                pass
    finally:
        gpio.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("--test", "--practice"):
        sys.exit(__doc__)
    _bring_up(sys.argv[1])
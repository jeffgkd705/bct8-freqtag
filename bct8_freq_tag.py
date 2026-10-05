#!/usr/bin/env python3
"""
Watches a Uniden BCT8 over its serial Remote port and tags the AirNav Radar
Icecast stream (fed separately by Darkice) with the currently active
frequency's label, using the scanner's documented ASCII remote protocol
(BCT8_Protocol_V201.pdf), verified against firmware v1.05.

Does not touch Darkice or the audio path at all -- this only updates Icecast
mount metadata out-of-band via the /admin/metadata endpoint, the same
mechanism rtl_airband's send_scan_freq_tags uses.
"""

import base64
import configparser
import logging
import os
import select
import signal
import termios
import time
import urllib.error
import urllib.parse
import urllib.request

SERIAL_PORT = "/dev/ttyUSB0"
BAUD = termios.B9600
DARKICE_CFG = "/etc/darkice.cfg"

# Controlled by the BCT8_LOG_LEVEL environment variable (DEBUG/INFO/WARNING/...),
# so verbosity can be changed via the systemd unit without touching this file.
# DEBUG logs every serial query/response and squelch poll; INFO (the default)
# only logs connects, squelch open/close transitions, and label changes.
logging.basicConfig(
    level=os.environ.get("BCT8_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(message)s",
)
log = logging.getLogger("bct8_freq_tag")

POLL_INTERVAL = 0.15       # seconds between SQ polls
COMMAND_TIMEOUT = 0.5      # seconds to wait for a command response
RECONNECT_DELAY = 5        # seconds to wait before reopening a dropped port
UNKNOWN_SUMMARY_INTERVAL = 24 * 60 * 60  # how often to log unlabeled frequencies seen

# Frequency (MHz) -> label, taken from the existing rtl_airband.conf channel list.
#
# To name a newly-seen frequency: add an entry here using the frequency exactly
# as logged (4 decimal places, MHz), e.g.:
#     123.4500: "Some New Label",
# then redeploy and restart the service (scp + install + systemctl restart,
# as documented in DEPLOYMENT.md).
FREQUENCY_LABELS = {
    124.9500: "RDU Approach",
    127.6750: "RDU Approach",
    128.3000: "RDU Approach",
    125.3000: "RDU Departure",
    132.3500: "RDU Departure",
    127.4500: "RDU Tower East Runways 05R/23L",
    119.3000: "RDU Tower West",
    118.9250: "ZDC U High @MYB",
    135.2000: "ZDC27 LIB Low",
    120.6250: "KJAC ATIS",
    122.7750: "FAA Support",
    132.2250: "ZDC38 Tar River High @RMT",
    132.6250: "Atlanta ARTCC",
    129.2250: "AAL Ops",
    130.1250: "Southwest",
    118.7500: "ZDC21 Dominion Low",
}
FREQUENCY_TOLERANCE_MHZ = 0.0005


def load_icecast_config(path):
    # darkice.cfg isn't valid INI (it has repeated [icecast2-N] style sections
    # with bare "key = value" lines), but configparser handles it fine as-is.
    parser = configparser.ConfigParser()
    parser.read(path)
    section = parser["icecast2-0"]
    return {
        "server": section.get("server").strip(),
        "port": section.getint("port"),
        "mount": section.get("mountpoint").strip(),
        "username": section.get("username").strip(),
        "password": section.get("password").strip(),
    }


def label_for_frequency(freq_mhz):
    """Returns the configured label, or None if freq_mhz isn't in FREQUENCY_LABELS."""
    for known_freq, label in FREQUENCY_LABELS.items():
        if abs(known_freq - freq_mhz) <= FREQUENCY_TOLERANCE_MHZ:
            return label
    return None


def update_icecast_metadata(icecast_cfg, song_text):
    mount = icecast_cfg["mount"]
    if not mount.startswith("/"):
        mount = "/" + mount
    url = (
        f"http://{icecast_cfg['server']}:{icecast_cfg['port']}/admin/metadata"
        f"?mount={urllib.parse.quote(mount)}"
        f"&mode=updinfo"
        f"&song={urllib.parse.quote(song_text)}"
    )
    req = urllib.request.Request(url)
    creds = f"{icecast_cfg['username']}:{icecast_cfg['password']}".encode()
    req.add_header("Authorization", "Basic " + base64.b64encode(creds).decode())
    log.debug("Icecast metadata request: mount=%s song=%r", mount, song_text)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read()
        log.debug("Icecast metadata response: HTTP %s %r", resp.status, body)
        return True
    except urllib.error.HTTPError as exc:
        log.error("Icecast metadata update failed: HTTP %s %s", exc.code, exc.reason)
    except urllib.error.URLError as exc:
        log.error("Icecast metadata update failed: %s", exc.reason)
    return False


class Bct8Serial:
    """Thin wrapper around the raw termios approach confirmed working against
    the BCT8 -- plain pyserial-style access did not reliably elicit a response
    during testing, but direct os/termios/select calls did."""

    def __init__(self, port, baud):
        self.port = port
        self.baud = baud
        self.fd = None

    def open(self):
        self.fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(self.fd)
        cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
        cc = attrs[6]
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 5
        termios.tcsetattr(
            self.fd, termios.TCSANOW, [0, 0, cflag, 0, self.baud, self.baud, cc]
        )
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        time.sleep(0.3)

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def query(self, cmd, timeout=COMMAND_TIMEOUT):
        start = time.time()
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        os.write(self.fd, (cmd + "\r").encode())
        total = b""
        deadline = start + timeout
        while time.time() < deadline:
            remaining = max(0.0, deadline - time.time())
            r, _, _ = select.select([self.fd], [], [], min(0.1, remaining))
            if r:
                chunk = os.read(self.fd, 256)
                if not chunk:
                    break
                total += chunk
                if total.endswith(b"\r"):
                    break
        result = total.strip()
        log.debug("serial TX %r -> RX %r (%.0fms)", cmd, result, (time.time() - start) * 1000)
        return result


def parse_ma_response(resp):
    # Expected: b"C038 F01249500 TF DF LF AF RF N000"
    text = resp.decode(errors="replace")
    if not text.startswith("C"):
        return None
    parts = text.split()
    if len(parts) < 2 or not parts[1].startswith("F"):
        return None
    try:
        channel = int(parts[0][1:])
        freq_digits = parts[1][1:]  # 8 digits, 1GHz digit .. 100Hz digit
        freq_mhz = int(freq_digits) / 10000.0
    except ValueError:
        return None
    return channel, freq_mhz


def log_unknown_summary(unknown_freqs, elapsed_seconds):
    elapsed_desc = f"{elapsed_seconds / 3600:.1f}h"
    if unknown_freqs:
        details = ", ".join(
            f"{freq:.4f} MHz (x{count})" for freq, count in sorted(unknown_freqs.items())
        )
        log.info("Unlabeled frequencies seen in the last %s: %s", elapsed_desc, details)
    else:
        log.info("No unlabeled frequencies seen in the last %s", elapsed_desc)


def run(icecast_cfg):
    radio = Bct8Serial(SERIAL_PORT, BAUD)
    last_squelch = None
    current_label = None
    open_confirmed = False
    unknown_freqs = {}
    last_unknown_summary = time.time()

    # `systemctl kill -s SIGUSR1 bct8-freqtag.service` triggers an on-demand
    # dump of unlabeled frequencies seen so far, without resetting the daily
    # timer/counters -- it's just a peek, independent of the once-a-day log.
    dump_requested = {"flag": False}

    def handle_sigusr1(signum, frame):
        dump_requested["flag"] = True

    signal.signal(signal.SIGUSR1, handle_sigusr1)

    while True:
        try:
            if radio.fd is None:
                log.info("Opening %s...", SERIAL_PORT)
                radio.open()
                info = radio.query("SI")
                log.info("Connected: %s", info.decode(errors="replace") or "(no SI reply)")
                last_squelch = None

            if dump_requested["flag"]:
                log.info("On-demand dump requested (SIGUSR1):")
                log_unknown_summary(unknown_freqs, time.time() - last_unknown_summary)
                dump_requested["flag"] = False

            if time.time() - last_unknown_summary >= UNKNOWN_SUMMARY_INTERVAL:
                log_unknown_summary(unknown_freqs, time.time() - last_unknown_summary)
                unknown_freqs.clear()
                last_unknown_summary = time.time()

            sq = radio.query("SQ")
            log.debug("squelch poll -> %r", sq)
            if sq not in (b"+", b"-"):
                time.sleep(POLL_INTERVAL)
                continue

            if sq != last_squelch:
                if sq == b"+":
                    ma = radio.query("MA")
                    parsed = parse_ma_response(ma)
                    if parsed is None:
                        # Common for very brief squelch breaks (blips, birdies) that
                        # close again before MA can confirm a channel -- not an error.
                        log.debug("Squelch opened but MA reply unparsed: %r", ma)
                        open_confirmed = False
                    else:
                        channel, freq_mhz = parsed
                        label = label_for_frequency(freq_mhz)
                        if label is None:
                            rounded = round(freq_mhz, 4)
                            unknown_freqs[rounded] = unknown_freqs.get(rounded, 0) + 1
                            label = f"{freq_mhz:.4f} MHz"
                        # Logged every confirmed transmission, even repeats of the same
                        # label; the Icecast push itself stays deduplicated below.
                        log.info("Squelch open: ch%d %.4f MHz -> %s", channel, freq_mhz, label)
                        open_confirmed = True
                        if label != current_label:
                            if update_icecast_metadata(icecast_cfg, label):
                                current_label = label
                else:
                    if open_confirmed:
                        log.info("Squelch closed")
                    else:
                        log.debug("Squelch closed")
                    open_confirmed = False
                last_squelch = sq

            time.sleep(POLL_INTERVAL)

        except OSError as exc:
            log.error("Serial error (%s); reconnecting in %ss", exc, RECONNECT_DELAY)
            radio.close()
            time.sleep(RECONNECT_DELAY)


def main():
    icecast_cfg = load_icecast_config(DARKICE_CFG)
    log.info(
        "Tagging mount %s on %s:%s (log level: %s)",
        icecast_cfg["mount"],
        icecast_cfg["server"],
        icecast_cfg["port"],
        logging.getLevelName(log.getEffectiveLevel()),
    )
    try:
        run(icecast_cfg)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

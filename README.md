# bct8-freqtag

Watches a Uniden BCT8 scanner over its serial Remote port and tags an
AirNav Radar Icecast VHF feed with the currently active frequency's name
(e.g. "RDU Approach", "ZDC27 LIB Low"), the same way RTLSDR-Airband's
`send_scan_freq_tags` does for SDR-based feeds -- but for a standalone
scanner radio feeding [Darkice](http://www.darkice.org/) instead.

## Why this exists

[Darkice](http://www.darkice.org/) happily streams a scanner's audio to
AirNav Radar, but it has no idea what frequency is currently active -- that
requires separately talking to the scanner. This script does exactly that,
entirely out-of-band from the audio path:

1. Polls the BCT8's squelch status over serial.
2. The instant squelch opens on a real transmission, asks the scanner what
   channel/frequency it's parked on.
3. Looks that frequency up in a small label table and pushes it to the
   Icecast mountpoint's metadata via the standard `/admin/metadata`
   out-of-band update -- the same mechanism most Icecast "now playing"
   tools use, independent of whatever is actually encoding/streaming the
   audio.

Darkice itself is never touched, restarted, or reconfigured by this tool.

## How it talks to the scanner

The BCT8's PC Control serial protocol isn't in the regular owner's manual --
it's documented separately by Uniden as an "Operation Specification /
Remote Command" PDF (search for `BCT8_Protocol_V201.pdf` on
`info.uniden.com`; not redistributed here due to copyright, but it's
Uniden's own published spec, not a reverse-engineered one). This project
was built and verified against a BCT8 running remote-command firmware
v1.05 specifically; a couple of the newer v2.00 commands documented in that
PDF (like the bare `RF` frequency-confirm query) don't work on that older
firmware, which is why this script uses `MA` (confirm current channel,
which returns both channel number and frequency together) instead.

The owner's manual itself (for the Remote Interface's physical/electrical
parameters -- baud rate options, 8N1, etc.) is also Uniden's copyrighted
material and isn't included here; see Uniden's own site or
[uniden.info](https://www.uniden.info/) for it.

## Files

- `bct8_freq_tag.py` -- the service itself.
- `bct8-freqtag.service` -- systemd unit, run alongside (not instead of)
  your existing `darkice.service`.
- `DEPLOYMENT.md` -- install, redeploy, monitoring, and rollback steps.

## Configuration

All configuration is in `bct8_freq_tag.py`:

- `FREQUENCY_LABELS` -- the frequency-to-label table. Ships with a real,
  working example for the Raleigh-Durham (RDU) area; replace with your own
  scanner's programmed frequencies and their identified names. See the
  comment directly above the table in the script for the exact steps to
  add a new one.
- `SERIAL_PORT`, `BAUD` -- defaults to `/dev/ttyUSB0` at 9600 baud, matching
  the BCT8's Remote-mode default.
- Icecast server/mount/credentials are **not** configured here -- they're
  read directly from `/etc/darkice.cfg` at startup, so there's exactly one
  place those live and nothing to keep in sync.

See `DEPLOYMENT.md` for the full install/setup walkthrough, including
prerequisites, troubleshooting a non-responsive serial link, and rollback.

## AI Notification
This application was vibe-coded with Claude Code. Caveat Emptor.

## License

MIT -- see `LICENSE`.

# Deployment Guide

This assumes you already have:
- A Raspberry Pi (or similar Linux box) running [Darkice](http://www.darkice.org/)
  feeding an AirNav Radar VHF mountpoint, configured via `/etc/darkice.cfg`
  (the standard `[icecast2-0]` section with `server`, `port`, `mountpoint`,
  `username`, `password`).
- A Uniden BCT8 (or compatible) scanner connected to that Pi over USB-to-serial,
  wired as a **straight-through RS-232 DB-9 cable** (not null-modem, and not a
  USB-TTL or "mobile phone data cable" adapter -- those use incompatible wiring
  or voltage levels).
- The scanner's audio already feeding Darkice's input independently (this tool
  never touches the audio path -- it only reads the scanner's serial status
  and pushes Icecast metadata).

## Prerequisites on the Pi

1. The user that will run this service must be in the **`dialout`** group
   (for `/dev/ttyUSB0` access) and able to read `/etc/darkice.cfg`. In many
   setups the user running Darkice itself is *not* in `dialout` (it usually
   only needs `audio`), so this is commonly a different, existing user on
   the box -- check with:
   ```
   groups <candidate-user>
   ```
2. Put the BCT8 into **Remote mode**: hold `RMT` on the front panel for ~2
   seconds, select a transfer speed (9600 is a safe default), confirm with
   `E`. This does not survive a scanner power-cycle -- it has to be redone
   by hand each time the scanner loses power.
3. Confirm the serial link works before installing the service:
   ```
   python3 -c "
   import os, termios, select, time
   fd = os.open('/dev/ttyUSB0', os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
   attrs = termios.tcgetattr(fd)
   cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
   cc = attrs[6]; cc[termios.VMIN] = 0; cc[termios.VTIME] = 10
   termios.tcsetattr(fd, termios.TCSANOW, [0,0,cflag,0,termios.B9600,termios.B9600,cc])
   os.write(fd, b'SI\r')
   time.sleep(0.5)
   print(os.read(fd, 64))
   "
   ```
   You should see something like `b'SI BCT8,0000000000,105\r'`. If you get
   nothing back, see the Troubleshooting section below before going further.

## Install

```
scp bct8_freq_tag.py <your-pi-hostname>:/tmp/
scp bct8-freqtag.service <your-pi-hostname>:/tmp/
ssh <your-pi-hostname>
sudo install -o root -g root -m 755 /tmp/bct8_freq_tag.py /usr/local/bin/bct8_freq_tag.py
sudo install -o root -g root -m 644 /tmp/bct8-freqtag.service /etc/systemd/system/bct8-freqtag.service
rm /tmp/bct8_freq_tag.py /tmp/bct8-freqtag.service
```

Edit `/etc/systemd/system/bct8-freqtag.service` and replace `<your-user>`
with the actual user from the Prerequisites step. Then:

```
sudo systemctl daemon-reload
sudo systemctl start bct8-freqtag.service
sudo systemctl status bct8-freqtag.service
```

If it looks healthy, enable it to survive reboots:
```
sudo systemctl enable bct8-freqtag.service
```
(Consider also enabling `darkice.service` itself if it isn't already, so the
whole feed survives a reboot: `sudo systemctl enable darkice.service`.)

## Redeploying after editing `FREQUENCY_LABELS`

Every time you add or change a frequency label in `bct8_freq_tag.py`:
```
scp bct8_freq_tag.py <your-pi-hostname>:/tmp/
ssh <your-pi-hostname> 'sudo install -o root -g root -m 755 /tmp/bct8_freq_tag.py /usr/local/bin/bct8_freq_tag.py && rm /tmp/bct8_freq_tag.py && sudo systemctl restart bct8-freqtag.service'
```

## Monitoring

```
journalctl -u bct8-freqtag.service -f        # live tail
journalctl -u bct8-freqtag.service -b        # since last boot
systemctl status bct8-freqtag.service        # quick health check
```

Default log level is `INFO` -- connects, squelch open/close (only for
confirmed transmissions, not brief blips), label changes, the once-a-day
unlabeled-frequency summary, and errors. Nothing more granular is written
by default, to keep SD card writes low on a Pi.

To get `DEBUG` (every serial query/response and every squelch poll):
```
sudo systemctl edit bct8-freqtag.service
# add under [Service]:
#   Environment=BCT8_LOG_LEVEL=DEBUG
sudo systemctl restart bct8-freqtag.service
```
Revert with `sudo systemctl revert bct8-freqtag.service` then restart again.

### On-demand unlabeled-frequency check

The service also logs a summary of any frequencies it saw that aren't in
`FREQUENCY_LABELS` once every 24 hours. To check sooner, without waiting or
resetting that daily timer:
```
sudo systemctl kill -s SIGUSR1 bct8-freqtag.service
journalctl -u bct8-freqtag.service -n 5
```

## Troubleshooting

- **No response at all over serial, any baud rate:** almost always a cable
  problem, not a scanner problem. In order of likelihood:
  1. Wrong cable type entirely (DB-9 "mobile phone data cables" use a
     proprietary pinout, not standard DTE TXD/RXD on pins 2/3).
  2. Null-modem wiring where straight-through is needed (PC Control mode
     wants straight-through; only BCT8-to-BCT8 Clone mode wants null-modem).
  3. Physical connector/mounting hardware preventing a full seat (loose
     DB-9 shells with the wrong screws can look connected but aren't).
  4. BCT8 not actually in Remote mode (see Prerequisites step 2) -- this
     resets on every scanner power-cycle.

  A quick way to isolate cable vs. scanner: short pins 2 and 3 together on
  the disconnected DB-9 end and try the same `SI` query above. A real reply
  means the cable/adapter loop back correctly and the issue is elsewhere
  (scanner state); silence even in loopback means the cable/adapter itself
  is bad or wired for something else.

- **`MA` reply comes back `NG` right after squelch opens:** normal for very
  brief squelch breaks (birdies, short blips) that close again before the
  scanner can confirm a channel. Logged at `DEBUG` only, not an error.

- **Service can't open `/dev/ttyUSB0` (permission denied):** the service's
  `User=` isn't in the `dialout` group. See Prerequisites step 1.

## Rollback

```
ssh <your-pi-hostname>
sudo systemctl stop bct8-freqtag.service
sudo systemctl disable bct8-freqtag.service
sudo rm /etc/systemd/system/bct8-freqtag.service
sudo systemctl daemon-reload
sudo rm /usr/local/bin/bct8_freq_tag.py
```
This removes everything this project adds and nothing else -- Darkice,
`/etc/darkice.cfg`, and all other existing services/files on the box are
untouched by install or rollback.

# CAN provisioning

These files set up the two CAN channels on the Pi. The owner installs them by hand, and only after approving them. Photoncar never installs them, and never changes CAN settings at runtime. It only reads them at startup.

## Files

| File | Interface | Settings |
|---|---|---|
| `80-can0.network` | `can0`, capture | 500 kbit/s, listen-only |
| `80-can1.network` | `can1`, requests | 500 kbit/s, one-shot, no automatic restart after bus-off, stays down until brought up by hand |

`can0` is listen-only, so the controller sends nothing: no frames, no ACK, no error flags. It captures the bus and doubles as the audit of every frame `can1` puts on the wire.

`can1` runs in normal mode and sends only allowlisted service 01 requests. `OneShot=yes` turns off automatic retransmit. `RestartSec=0` is the systemd-networkd form of `ip link ... restart-ms 0`, so a bus-off stays off. `ActivationPolicy=manual` keeps `can1` down at boot.

## Install (owner only, after approval)

First confirm the Pi's systemd knows every key these files use. `man systemd.network` must list `ListenOnly=`, `OneShot=`, and `RestartSec=` under `[CAN]`, and `ActivationPolicy=` under `[Link]`. An unknown key is ignored with a warning, which would leave a setting off.

```sh
sudo install -m 0644 80-can0.network 80-can1.network /etc/systemd/network/
sudo systemctl enable --now systemd-networkd
sudo networkctl reload
ip -details link show can0    # expect LISTEN-ONLY in the ctrlmode line
```

Bring `can1` up only when OBD polling is approved and `[can] obd_polling = true` is set in `config/photoncar.toml`:

```sh
sudo networkctl up can1
ip -details link show can1    # expect ONE-SHOT and restart-ms 0
```

## What Photoncar checks

- At startup it reads `ip -details -json link show can0` and refuses to capture unless `can0` is up with `LISTEN-ONLY` set.
- It opens `can1` only when `obd_polling = true`, and never on the capture interface.
- Before it opens `can1`, it reads `ip -details -json link show can1`. It refuses unless `can1` is an up CAN interface with `ONE-SHOT` on, `LISTEN-ONLY` off, restart-ms 0, and the bitrate in `[can] bitrate`.
- While polling, it reads the `can1` error counters once a second (`ip -details -statistics -json link show can1`). Any rise, a failed read, or the watch stopping turns requests off for the rest of the session. No request goes out while the watch is not running.
- Every frame on `can0` goes through the TX audit. Requests turn off for the session on a frame that fails the allowlist, a request Photoncar did not send, a request `can0` never saw, an error frame, a retransmit, or a rate over the cap.
- It never runs `ip link set`, `networkctl`, or any other command that changes an interface.

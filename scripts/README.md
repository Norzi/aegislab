# VPN node automation scripts

Keeps a headless sing-box server connected through VPN nodes that get blocked within hours.

| File | What it does |
|---|---|
| `vpn_autopilot.py` | Stops sing-box, fetches the subscription, builds a new sing-box config, speed-tests every node (real download, not ping), switches to the fastest one. Rolls back to the last working config if anything fails |
| `vpn_watchdog.py` | Every 2 minutes sends a real HTTPS request through the tunnel. After 3 failures in a row it starts the autopilot (with a 10-minute cooldown) |
| `systemd/` | Service and timer units: autopilot every 90 minutes, watchdog every 2 minutes |
| `autopilot.env.example` | Template for the private values (subscription URL, device ID, fixed node) |

## Install

```bash
sudo mkdir -p /opt/singbox
sudo cp vpn_autopilot.py vpn_watchdog.py /opt/singbox/
sudo cp autopilot.env.example /etc/sing-box/autopilot.env
sudo chmod 600 /etc/sing-box/autopilot.env     # then edit it with your real values
sudo cp systemd/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vpn-autopilot.timer vpn-watchdog.timer
journalctl -u vpn-autopilot -f                 # watch it work
```

## Design notes

- **Why stop sing-box before fetching:** with a dead config and `auto_route` on, the subscription fetch itself goes into the broken tunnel (chicken-and-egg). The autopilot tears sing-box down, fetches over the physical interface, then starts it again.
- **Safety rails:** lock file so two runs never overlap, atomic config writes, automatic rollback, cooldown in the watchdog so a dead subscription cannot cause a restart loop.
- **Two paths:** port 8080 uses the best subscription node; port 18081 is pinned to one fixed node. Used by the multi-path download setup described in the [6+6 write-up](https://github.com/Norzi/aegisnet/tree/main/multipath).
- **Not supported:** XHTTP nodes are skipped because upstream sing-box has no XHTTP transport.

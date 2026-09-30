# Hardening what the dashboard reports

The dashboard *shows* exposure; it does not change anything. These notes explain the two findings that surprise people, and the optional helper scripts.

## Docker ports bypass ufw

Docker publishes a port by inserting rules in `nat` and `FORWARD` **before** the chains ufw manages. `ufw deny 5432` therefore
does nothing for a container published with `-p 5432:5432`. The dashboard marks such ports `docker: bypasses ufw` and raises an alarm when a database is one of them.

Real fixes, in order of preference:

1. **Bind to loopback**: publish with `-p 127.0.0.1:5432:5432` (compose: `"127.0.0.1:5432:5432"`). Nothing outside can reach it; other containers still reach it through the Docker network.
2. Put the rules in the `DOCKER-USER` chain (the only chain Docker promises not to rewrite).
3. Do not publish the port at all; let containers talk over a user-defined network.

`scripts/rebind-all-dbs.sh [--dry-run] "NAME HOST_PORT CONTAINER_PORT" ...` re-creates containers that were started with `docker run` so that the port
is bound to 127.0.0.1, with a volume backup and a data check; it refuses anything it cannot reproduce faithfully and restores the original container if a step fails after the stop.
**Read the script and use `--dry-run` first.**

## Tailscale is accepted before ufw

`tailscaled` installs a `ts-input` chain that accepts traffic arriving on `tailscale0` ahead of ufw. Any service listening on a non-loopback address is
reachable by every device in your tailnet whatever ufw says. Restrict it with Tailscale ACLs, or bind the service to 127.0.0.1 and expose it on purpose through `tailscale serve`.

## ufw that is "active" but not filtering

`systemctl is-active ufw` only tells you that the unit that *loads* the rules ran. The real state is `ufw status verbose` / `iptables -S`. The dashboard reads the latter.

## Enabling ufw safely

```bash
LAN=192.168.0.0/24 ./scripts/enable-ufw.sh --dry-run     # show the rules
sudo LAN=192.168.0.0/24 ./scripts/enable-ufw.sh          # apply
```

The script allows SSH from your LAN, all of `tailscale0` and Tailscale's direct-connection UDP port, denies other incoming traffic, and arms a **systemd timer that switches ufw off again
after 300 s unless you confirm** from a second SSH session, so a wrong rule can't lock you out. `LAN` is mandatory (there is no default).
After a confirmed change, run `sudo nuc-console-accept` so the new state becomes the baseline.

# UniFi: Allow external access to www.klikk.co.za (ports 80 & 443)

If your server is **behind a UniFi gateway** (USG, Dream Machine, etc.), the public IP (e.g. 102.135.240.222) is on the UniFi device. For https://www.klikk.co.za to work from the internet you need:

1. **Port forwarding (NAT)** – forward WAN ports 80 and 443 to the **server’s LAN IP** and the same ports.
2. **Firewall** – allow WAN → LAN traffic on 80 and 443 (or allow the port-forward rule).

---

## 1. Port forwarding (UniFi Controller)

- **UniFi Controller** → **Settings** (or **Network**) → **Firewall & Security** or **Routing & Firewall** (name depends on controller version).
- Find **Port Forwarding** / **NAT** / **Forwarding**.
- Add two rules:

| Name        | From (WAN) | To (LAN)        | Protocol |
|------------|------------|------------------|----------|
| HTTP       | Any / WAN  | Server LAN IP:80 | TCP 80   |
| HTTPS      | Any / WAN  | Server LAN IP:443| TCP 443  |

- **Forward IP**: your server’s LAN IP (e.g. `192.168.1.37` – same as in the Nginx error log).
- **Forward port**: 80 for the first rule, 443 for the second (or “Same as incoming” if the option exists).

Older UI: **Settings** → **Routing & Firewall** → **Port Forwarding** → Create rule: Public Port 443, Private IP = server, Private Port 443, Protocol TCP. Repeat for 80.

---

## 2. Firewall (UniFi)

- Ensure the firewall does **not** block **inbound** traffic from WAN to the server on ports 80 and 443.
- Some setups have a “Allow established/related” or “Allow port forward” that automatically allows traffic that matches a port-forward rule. If not, add a **WAN IN** rule: allow TCP 80 and 443 to the server’s LAN IP.

---

## 3. Quick check

- From the server (or a machine on the same LAN), you already see **200** for:
  `curl -sI -k -H "Host: www.klikk.co.za" https://127.0.0.1/`
- From **another network** (e.g. mobile data or a different Wi‑Fi):
  `curl -sI https://www.klikk.co.za`
  - If it still **times out**, the block is between the internet and your server – almost certainly UniFi (no port forward or firewall block).
  - If you get **200** or **301/302**, the site is reachable; try in a browser.

---

## Summary

| Item | Action |
|------|--------|
| Port forward 80  | WAN:80 → Server LAN IP:80 (TCP) |
| Port forward 443 | WAN:443 → Server LAN IP:443 (TCP) |
| Firewall        | Allow WAN → Server on 80, 443 (or allow the port-forward) |

The server’s LAN IP is the one your Ubuntu box has on the same network as the UniFi (e.g. `ip addr` or `hostname -I` on the server). Use that IP in the UniFi port-forward and firewall rules.

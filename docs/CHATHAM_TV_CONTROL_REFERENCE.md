# Chatham TV Control — Roku TVs + DirecTV Boxes (from the Beelink)

> Chatham-specific reference. Control every TV and DirecTV receiver at Chatham
> from the Beelink over the network. Two systems, two ports:
>
> - **ROKU TVs** (inputs/apps/power) → **PORT 8060** (4 TVs)
> - **DIRECTV boxes** (channels) → **PORT 8080** (6 receivers)
>
> **DON'T CROSS THEM:** a channel number only works against a DirecTV box (8080);
> an input/app command only works against a Roku (8060). A Roku has no concept of
> DirecTV channels and vice versa. Commands are run FROM the Beelink (ssh in first).
> All are curl one-liners.

---

## PART 1 — ROKU TVs (port 8060)

### Roku map

| IP | Name | Model | Size | WiFi MAC | Notes |
|----|------|-------|------|----------|-------|
| 10.1.10.84  | Bar Left       | Roku Plus 4K | 55" | d4:be:dc:77:0f:a3 | DirecTV on HDMI 1 |
| 10.1.10.88  | Bar Right      | 50R4AX       | 50" | 4c:50:dd:d3:ae:71 | |
| 10.1.10.118 | Chat DR Right  | 65R4A5R      | 65" | b8:ab:62:b1:01:37 | |
| 10.1.10.154 | Chat DR Middle | 65R4A5R      | 65" | a8:16:9d:33:1b:7b | |

All on WiFi SSID **CBCI-6AB6**.

Requires on each TV: `Settings > System > Advanced system settings > Control by mobile apps = Enabled` (on by default).

**RELIABILITY TIP:** turn ON `Settings > System > Power > Fast TV Start` on every Roku. With it off, a sleeping Roku drops its 8060 port (still pings, but won't answer commands) and vanishes from scans until woken with the remote. Fast TV Start keeps it reachable.

### Pin Roku IPs so they don't drift (DHCP reservations on the router)

Roku IPs are DHCP and can change on a lease renewal, breaking the commands below until someone re-scans (the .84 TV already jumped .87 → .84 once). Bind each MAC to its IP on the router:

```
d4:be:dc:77:0f:a3  ->  10.1.10.84    (Bar Left / DirecTV)
4c:50:dd:d3:ae:71  ->  10.1.10.88    (Bar Right)
b8:ab:62:b1:01:37  ->  10.1.10.118   (Chat DR Right)
a8:16:9d:33:1b:7b  ->  10.1.10.154   (Chat DR Middle)
```

### Confirm a Roku is reachable / see what it is

```bash
curl -s --max-time 5 "http://<TV_IP>:8060/query/device-info" | grep -E 'friendly-device-name|model-name|power-mode'
```

Times out = asleep (8060 down) or wrong IP. Wake with remote (Home) and retry, or turn on Fast TV Start.

### List inputs / apps on a Roku

```bash
curl -s "http://<TV_IP>:8060/query/apps"
```

Inputs = `tvinput.hdmi1`, `tvinput.hdmi2`, `tvinput.dtv` (built-in tuner). Apps = numeric IDs. Match case exactly — IDs are lowercase.

### Switch input (e.g. turn on the DirecTV box, works even if tile missing)

```bash
curl -d '' "http://<TV_IP>:8060/launch/tvinput.hdmi1"
# Example - DirecTV on Bar Left:
curl -d '' "http://10.1.10.84:8060/launch/tvinput.hdmi1"
```

### Launch an app (id from /query/apps)

```bash
curl -d '' "http://<TV_IP>:8060/launch/<app_id>"
# Netflix = 12, YouTube = 837 (verify per TV via /query/apps)
```

### Send a remote keypress

```bash
curl -d '' "http://<TV_IP>:8060/keypress/<KEY>"
# Keys: Power PowerOff PowerOn | VolumeUp VolumeDown VolumeMute |
#       Home Back Select | Up Down Left Right | Play Rev Fwd
# Turn a TV off:  curl -d '' "http://10.1.10.88:8060/keypress/PowerOff"
# Turn it on:     curl -d '' "http://10.1.10.88:8060/keypress/PowerOn"
```

### Re-scan for Rokus (parallel; prints only what it finds)

```bash
for ip in $(seq 2 254); do (
  r=$(curl -s --max-time 1 "http://10.1.10.$ip:8060/query/device-info" | grep -o '<friendly-device-name>[^<]*' | cut -d'>' -f2)
  [ -n "$r" ] && echo "10.1.10.$ip  ROKU: $r"
) & done; wait; echo "---- done ----"
```

A missing Roku is almost always asleep with Fast TV Start off — pings but won't answer. Turn it on and re-scan.

---

## PART 2 — DIRECTV BOXES (port 8080)

Uses DirecTV's built-in SHEF API. Each receiver drives one TV.

### DirecTV map

| IP | Name | Notes |
|----|------|-------|
| 10.1.10.29  | BAR RT     | |
| 10.1.10.58  | BAR MIDDLE | |
| 10.1.10.87  | BAR LFT    | feeds the Bar Left Roku, HDMI 1 |
| 10.1.10.93  | DR MID     | |
| 10.1.10.199 | DR RT      | |
| 10.1.10.208 | DR LEFT    | |

Bar = 3 boxes (RT / MIDDLE / LFT), Dining room = 3 (MID / RT / LEFT).

### Change a channel

```bash
curl -s "http://<BOX_IP>:8080/tv/tune?major=<CHANNEL>"
# Example - ESPN on BAR LFT:
curl -s "http://10.1.10.87:8080/tv/tune?major=206"
```

Returns JSON with `"code": 200` on success; TV flips a second later.

### See what's on a TV now

```bash
curl -s "http://<BOX_IP>:8080/tv/getTuned"
```

Read `major` (channel) and `callsign` (station). The `title` field is the current program/ad, NOT the channel — trust callsign + major.

### Confirmed channel numbers (this market)

```
ESPN        206
NESN HD     628
Fox News    360
```

To capture more: tune a box to the channel with the remote, run `getTuned`, record the `major` number it reports.

### Re-scan for DirecTV boxes (parallel; prints only what it finds)

```bash
for ip in $(seq 2 254); do (
  n=$(curl -s --max-time 1 "http://10.1.10.$ip:8080/info/getLocations" | grep -o '"locationName": "[^"]*"' | cut -d: -f2- | tr -d ' "')
  [ -n "$n" ] && echo "10.1.10.$ip  DTV: $n"
) & done; wait; echo "---- done ----"
```

A missing box is usually POWERED OFF / in standby — it drops off the network until the TV is turned on. Turn it on and re-scan.

### Firmware / longevity note

SHEF (port 8080) is undocumented and DirecTV has been quietly retiring it on newer Gemini boxes. No supported way to block a firmware update, and updates come down the dish feed (not just the network) — don't try to firewall it. Hedge instead: keep the PHYSICAL REMOTES on-site as the real fallback. If SHEF ever dies, a cheap network IR blaster (Global Cache iTach / Broadlink RM4) aimed at the box replicates remote presses.

```bash
# Snapshot a box's firmware:
curl -s "http://10.1.10.87:8080/info/getVersion"
```

---

## OTHER TVs — SAMSUNG + VIZIO (not scripted, on purpose)

Chatham also has 1 Samsung and 1 Vizio. Not set up for Beelink control:

- **SAMSUNG:** encrypted WebSocket API (8001/8002); first command triggers an allow/deny popup ON THE TV that someone must accept once. Can't be done headless.
- **VIZIO SmartCast:** needs a PIN-pairing handshake (code shows on the TV, fed back for a token). Also needs someone at the set; may not even be on the network.

For one of each in a bar, scripting isn't worth the pairing hassle or the maintenance risk. Use the PHYSICAL REMOTE — label it, keep it in the drawer. Revisit only if either gets mounted somewhere unreachable.

---

## Quick reference

```bash
# Turn on DirecTV on Bar Left TV:
curl -d '' "http://10.1.10.84:8060/launch/tvinput.hdmi1"

# Put ESPN on the Bar Left screen (its DirecTV box is .87):
curl -s "http://10.1.10.87:8080/tv/tune?major=206"

# Turn Bar Right TV off / on:
curl -d '' "http://10.1.10.88:8060/keypress/PowerOff"
curl -d '' "http://10.1.10.88:8060/keypress/PowerOn"

# See what's on any dining-room TV:
curl -s "http://10.1.10.93:8080/tv/getTuned"      # DR MID
curl -s "http://10.1.10.199:8080/tv/getTuned"     # DR RT
curl -s "http://10.1.10.208:8080/tv/getTuned"     # DR LEFT
```

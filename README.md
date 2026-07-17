# Dont use this yet, it is W.I.P .
# RPC for [Youtube Music](https://music.youtube.com)
Since youtube does not seem to have a rich presence feature, i created this workaround and decided to publish it in case it helps others (i use [Vesktop btw](https://vesktop.dev/)).

If Youtube Music ever has a rich presence feature then this repository will be archived.

> [!WARNING]
> This script assumes that you only use 1 tab for listening to songs on Youtube Music. Having several songs playing at once with different youtube music tabs will cause issues with the RPC.

## How to install?
> [!NOTE]
> This assumes you have Python 3+ installed and are using systemd (creator of this repo uses [CachyOS](https://cachyos.org/) alongside [LibreWolf](https://www.librewolf.net/))
1. Install [Tampermonkey](https://addons.mozilla.org/firefox/addon/tampermonkey/)
2. Install the [tampermonkey_userscript](src/tampermonkey_script.js)
3. Download [ytmusic_bridge.py](src/ytmusic_bridge.py) and save it somewhere convenient (this guide assumes `~/Documents`).
> [!WARNING]
> If you saved `ytmusic_bridge.py` somewhere else, edit this in `ytmusic-bridge.service`:
> ```bash
> %h/Documents/ytmusic_bridge.py
> ```
> with the proper location to ytmusic_bridge.py
4. Download [ytmusic-bridge.service](src/ytmusic-bridge.service) and save it in `~/.config/systemd/user/`.
5. Run this in your Terminal
```
systemctl --user enable --now ytmusic-bridge.service
```

## How to uninstall once installed?
1. Run
```
systemctl --user disable --now ytmusic-bridge.service
```
> [!TIP]
> You may also run the following after the service is disabled
> ```
> pkill -f ytmusic_bridge.py 2>/dev/null
> ```
2. Delete `ytmusic-bridge.service` and `ytmusic_bridge.py`
3. Uninstall the [tampermonkey_userscript](src/tampermonkey_script.js)

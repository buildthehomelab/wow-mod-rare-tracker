# Rare Tracker

An [AzerothCore](https://www.azerothcore.org/) (WotLK 3.3.5a) module that keeps track of every
rare and rare elite in the open world and serves the list from memory over HTTP, for a **live
rare map** on your realm's website. It can also keep **playerbots off rares**, so the rares are
still there when real players come looking.

- **Live and in memory.** The worldserver answers `GET /rares.json` itself. Nothing is written
  to disk, and the list is only rebuilt while someone is looking at it.
- **Honest about what it knows.** A rare in a loaded grid (someone is nearby) is reported as it
  really is: alive or dead, where it has wandered to, its health and whether it's in combat. A
  rare nobody is near is "up" unless the map has a respawn timer pending for it.
- **Only real spawns.** Pooled rares show at the spot the pool picked, holiday and event spawns
  only while their event runs, and nothing phased away. Instances are left out.
- **Bots leave rares alone.** Playerbots that aren't grouped with a real player see open-world
  rares as friendly, so they can't attack them and the rares don't aggro them.

The matching web page is in [wow-mod-azerothcore-portal](https://github.com/buildthehomelab/wow-mod-azerothcore-portal)
(`rares.php`). It has continent and zone maps, a searchable list, and respawn countdowns.

## Requirements

- An AzerothCore WotLK server (`azerothcore-wotlk`, master). Stock core only; the module needs no
  SQL and no client patch.
- Optional: [mod-playerbots](https://github.com/mod-playerbots/mod-playerbots) (with its
  AzerothCore fork) for `RareTracker.BotsIgnoreRares`; it does nothing without bots.
- Optional: the [wow-mod-azerothcore-portal](https://github.com/buildthehomelab/wow-mod-azerothcore-portal)
  website for the rare map page (`rares.php`), or anything else that can read `/rares.json`.
- Optional, for the map images: Python 3 with Pillow and a WoW 3.3.5a (12340) client's `Data`
  folder.

## Install

The folder name matters: AzerothCore derives the loader symbol from it.

```bash
cd ~/azerothcore-wotlk/modules
git clone https://github.com/buildthehomelab/wow-mod-rare-tracker.git mod-rare-tracker
```

Copy `conf/mod_rare_tracker.conf.dist` to your modules config folder (in Docker,
`env/dist/etc/modules/mod_rare_tracker.conf`), then rebuild and restart the worldserver. No SQL
is needed.

On startup the log shows something like:

```
>> mod-rare-tracker: <n> rare spawns of <n> rares in <n> zones (worked out <n> zones) in <n> ms
>> mod-rare-tracker: serving rares on http://0.0.0.0:8095/rares.json
```

### Reaching it from the website

The endpoint listens inside the worldserver container. Other containers on the AzerothCore
network reach it by container name, so **no port has to be published** on the host:

```bash
docker exec wow-register php -r 'echo file_get_contents("http://ac-worldserver:8095/health");'
```

In the portal's `.env`, set:

```ini
RARE_TRACKER_URL=http://ac-worldserver:8095/rares.json
```

The portal passes the list through `api/rares.php`, so browsers never talk to the worldserver.

## Map images

The page draws rares on the game's own world maps. Those are Blizzard art, so they aren't in
this repo. Build them once from your WoW client with `tools/build_worldmaps.py`. It reads the
client's MPQs directly (patches included), on Windows, macOS or Linux, and only needs Python 3
and Pillow.

On Windows, from the `tools` folder:

```bat
py -m pip install pillow
py build_worldmaps.py --mpq-dir "C:\World of Warcraft\Data" --out worldmap
```

On macOS or Linux:

```bash
python3 -m pip install pillow
python3 tools/build_worldmaps.py --mpq-dir /path/to/WoW/Data --out worldmap
```

It prints one line per map and ends with `4 continents and N zones written to worldmap`. Copy
the `worldmap` folder into the portal's `realm-public` folder (`REALM_CONFIG_DIR`), so it's
served at `/realm/worldmap/`; no rebuild is needed.

The tool paints every explored-area overlay onto each zone, so maps look fully explored. It
writes one `zone-<id>.jpg` per zone, one `continent-<mapId>.jpg` per continent, and `maps.json`
with each continent's edges. If you've already extracted `Interface\WorldMap` and
`DBFilesClient` from the MPQs, pass that folder with `--extracted` instead of `--mpq-dir`.
Without the images, the page still works: it lists every rare and places them on a plain grid.

## Configuration

| Setting | Default | |
|---|---|---|
| `RareTracker.Enable` | `1` | Master switch. |
| `RareTracker.BotsIgnoreRares` | `1` | Keep ungrouped playerbots off open-world rares (see below). |
| `RareTracker.RefreshSeconds` | `30` | How often the list is rebuilt while someone is watching (min. 5). |
| `RareTracker.IdleSeconds` | `120` | Stop rebuilding when nobody has asked for this long. |
| `RareTracker.IncludeEntries` | `""` | Comma-separated creature entries to track although they aren't ranked rare (e.g. `17591`, Blood Elf Bandit). Restart to change. |
| `RareTracker.ExcludeEntries` | `""` | Comma-separated creature entries that are ranked rare but aren't really rares. Restart to change. |
| `RareTracker.Http.Enable` | `1` | Serve the list. |
| `RareTracker.Http.BindAddress` | `0.0.0.0` | `0.0.0.0` in Docker; `127.0.0.1` to keep it local outside Docker. |
| `RareTracker.Http.Port` | `8095` | |
| `RareTracker.Http.AllowOrigin` | `*` | `Access-Control-Allow-Origin` header; empty leaves it out. |

The HTTP settings are read at startup only.

## Bots and rares

With `BotsIgnoreRares = 1`, a playerbot with **no real player in its group** (and its pets,
totems and guardians) sees every open-world rare as friendly, and the rare sees the bot the
same way:

- the bot can't target it, and grinding bots pass it by;
- its AoE skips the rare;
- the rare doesn't aggro the bot when it wanders past.

Damage from such a bot to a rare is also set to 0. This catches anything that doesn't check
reactions, and fights that started while the bot was still in a player's group.

Bots **grouped with a real player** fight rares as normal, so you can still take your bot party
rare hunting. Rares inside dungeons and raids aren't affected.

Playerbots already skips rares when solo bots pick grind targets. The rare deaths this stops come
from bot-only groups, from bots defending themselves when a rare aggroes them, and from stray AoE.

## The JSON

`GET /rares.json` (also `/`; `/health` answers `ok`):

```json
{
  "generated": 1790861234, "refresh": 30, "up": 143,
  "zones": { "10": "Duskwood" },
  "rares": [
    { "spawn": 4567, "entry": 522, "name": "Mor'Ladim", "minLevel": 35, "maxLevel": 35, "elite": true,
      "map": 0, "zone": 10, "wx": -10970.1, "wy": 288.4, "x": 46.31, "y": 79.12,
      "state": "up", "respawnSeconds": 9000,
      "live": true, "level": 35, "hp": 100, "inCombat": false }
  ]
}
```

| Field | Meaning |
|---|---|
| `x`, `y` | Zone map coordinates, as the in-game map shows them (`null` if the zone has no map). |
| `zone`, `area` | The zone whose map shows the rare. A rare at a dungeon entrance or in Dalaran is shown on the surrounding zone's map, as in game, and `area` is the place it's really in (e.g. zone Westfall, area The Deadmines). |
| `wx`, `wy` | World coordinates. |
| `state` | `up` or `dead`. |
| `respawnAt` | When a dead rare comes back (unix time), if known. |
| `live` | `true` when the rare's grid is loaded, so its position and health are real. `false` means it's at its spawn point as far as anyone knows. |

Every snapshot is rebuilt on the world thread between map updates. The HTTP server runs on its
own thread and only hands out the last snapshot, so requests never touch game state.

## Troubleshooting

- **The website shows no rares.** Check that the portal can reach the worldserver. From another
  container on the AzerothCore network, `http://ac-worldserver:8095/health` should answer `ok`,
  and the portal's `RARE_TRACKER_URL` must point at `http://ac-worldserver:8095/rares.json`.
- **Changing the HTTP settings has no effect.** `RareTracker.Http.*` is read at startup only.
  Restart the worldserver. Outside Docker, set `RareTracker.Http.BindAddress` to `127.0.0.1` to
  keep it local.
- **Rares sit on a plain grid with no map.** The map images aren't built. Run
  `tools/build_worldmaps.py` against your client and copy the `worldmap` folder into the portal's
  `realm-public` folder.

## Credits

Author: [buildthehomelab](https://github.com/buildthehomelab)

## License

MIT. See [LICENSE](LICENSE).

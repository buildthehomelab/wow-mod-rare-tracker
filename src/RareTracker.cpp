/*
 * mod-rare-tracker
 *
 * Tracks every rare and rare elite on the open-world maps (no instances) and serves the list as
 * JSON over HTTP straight from memory, for a live rare map on the realm's website:
 *
 *   GET http://<worldserver>:8095/rares.json
 *
 * Nothing is written to disk. The list is rebuilt on the world thread, between map updates, at
 * most every RefreshSeconds, and only while someone has asked for it in the last IdleSeconds.
 * With nobody watching, the module costs nothing.
 *
 * A rare whose grid is loaded is reported as it really is: alive or dead, where it has wandered
 * to and its health. A rare in an unloaded grid is "up" unless the map has a respawn time pending
 * for it. Only spawns the server would actually load count: pooled rares at the spot the pool
 * picked, event spawns while their event runs, and nothing phased away.
 *
 * It can also keep playerbots off rares (BotsIgnoreRares). A bot that isn't grouped with a real
 * player, and its pets, see every open-world rare as friendly: they can't target it, their AoE
 * skips it and the rare won't aggro them. Bots in a real player's group fight rares as normal.
 *
 * Released under the MIT License.
 */

#include "SnapshotHttpServer.h"

#include "Config.h"
#include "Creature.h"
#include "DBCStores.h"
#include "DatabaseEnv.h"
#include "GameTime.h"
#include "Group.h"
#include "GroupReference.h"
#include "Log.h"
#include "Map.h"
#include "MapMgr.h"
#include "ObjectMgr.h"
#include "Player.h"
#include "PoolMgr.h"
#include "ScriptMgr.h"
#include "StringFormat.h"
#include "Timer.h"
#include "World.h"
#include "WorldSession.h"

#include <algorithm>
#include <map>
#include <sstream>
#include <string>
#include <type_traits>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace
{
    struct Config
    {
        bool enabled = true;
        bool botsIgnoreRares = true;
        bool httpEnabled = true;
        std::string httpAddress = "0.0.0.0";
        uint16 httpPort = 8095;
        std::string allowOrigin = "*";
        uint32 refreshSeconds = 30;
        uint32 idleSeconds = 120;
        std::unordered_set<uint32> excludedEntries;
    } config;

    struct RareKind
    {
        std::string name;
        uint8 minLevel = 0;
        uint8 maxLevel = 0;
        bool elite = false;
    };

    struct RareSpawn
    {
        ObjectGuid::LowType spawnId = 0;
        uint32 entry = 0;
        uint16 map = 0;
        float x = 0.0f;
        float y = 0.0f;
        uint32 zone = 0;
        uint32 gridId = 0;
        uint32 poolId = 0;
        uint32 spawnGroupId = 0;
        uint32 respawnSeconds = 0;
    };

    struct Index
    {
        std::unordered_map<uint32, RareKind> kinds;  // creature entry -> what it is
        std::vector<RareSpawn> spawns;
        std::map<uint32, std::string> zoneNames;      // zone id -> name, filled in as zones come up
    } rareIndex;

    SnapshotHttpServer httpServer;
    int64 lastBuild = 0;

    // --- Telling rares and bots apart ---------------------------------------------------------

    // The playerbots fork adds WorldSession::IsBot(); stock AzerothCore doesn't have it. Looking for
    // it at compile time lets the module build on both.
    template <typename Session, typename = void>
    struct HasIsBot : std::false_type { };

    template <typename Session>
    struct HasIsBot<Session, std::void_t<decltype(std::declval<Session&>().IsBot())>> : std::true_type { };

    template <typename Session>
    bool IsBotSession(Session* session)
    {
        if constexpr (HasIsBot<Session>::value)
            return session && session->IsBot();
        else
            return false;
    }

    bool IsRareRank(uint32 rank)
    {
        return rank == CREATURE_ELITE_RARE || rank == CREATURE_ELITE_RAREELITE;
    }

    bool IsOpenWorldMap(uint32 mapId)
    {
        MapEntry const* map = sMapStore.LookupEntry(mapId);
        return map && map->IsWorldMap();
    }

    // Test and placeholder creatures that Blizzard left in the data.
    bool LooksLikePlaceholder(std::string const& name)
    {
        return name.empty() || name.front() == '[' || name.find("DND") != std::string::npos
            || name.find("(PH)") != std::string::npos;
    }

    // A rare out in the open world (not a pet or anything else a player controls). This runs on
    // every reaction check, so the cheap tests go first.
    bool IsOpenWorldRare(Unit const* unit)
    {
        if (!unit->IsCreature() || unit->IsControlledByPlayer())
            return false;

        CreatureTemplate const* proto = unit->ToCreature()->GetCreatureTemplate();
        return proto && IsRareRank(proto->rank) && IsOpenWorldMap(unit->GetMapId())
            && !config.excludedEntries.count(proto->Entry);
    }

    // A bot, or a bot's pet, totem or guardian, with no real player in its group.
    bool IsFreeRoamingBot(Unit const* unit)
    {
        Player const* player = unit->GetAffectingPlayer();
        if (!player || !IsBotSession(player->GetSession()))
            return false;

        Group const* group = player->GetGroup();
        if (!group)
            return true;

        for (GroupReference const* ref = group->GetFirstMember(); ref; ref = ref->next())
            if (Player const* member = ref->GetSource())
                if (member->GetSession() && !IsBotSession(member->GetSession()))
                    return false;

        return true;
    }

    bool KeepBotOffRare(Unit const* a, Unit const* b)
    {
        return (IsOpenWorldRare(a) && IsFreeRoamingBot(b)) || (IsOpenWorldRare(b) && IsFreeRoamingBot(a));
    }

    // --- Index of rare spawns, built once at startup -----------------------------------------

    char const* DbcString(char const* const (&names)[16])
    {
        char const* name = names[sWorld->GetDefaultDbcLocale()];
        return name && *name ? name : names[LOCALE_enUS];
    }

    // Name of a zone, looked up once and kept. Only used on the world thread.
    std::string const& ZoneName(uint32 zoneId)
    {
        auto itr = rareIndex.zoneNames.find(zoneId);
        if (itr != rareIndex.zoneNames.end())
            return itr->second;

        AreaTableEntry const* area = sAreaTableStore.LookupEntry(zoneId);
        return rareIndex.zoneNames[zoneId] = area ? DbcString(area->area_name) : "";
    }

    // Zone ids the database already has (the core fills them in when
    // Calculate.Creature.Zone.Area.Data is on, and mod-npc-finder does for its NPCs). The casts pin
    // the column types, which the Field getters check.
    std::unordered_map<uint32, uint32> LoadSavedZones()
    {
        std::unordered_map<uint32, uint32> zones;
        if (QueryResult result = WorldDatabase.Query("SELECT CAST(guid AS UNSIGNED), CAST(zoneId AS UNSIGNED) FROM creature WHERE zoneId <> 0"))
            do
            {
                Field* fields = result->Fetch();
                zones[uint32(fields[0].Get<uint64>())] = uint32(fields[1].Get<uint64>());
            } while (result->NextRow());
        return zones;
    }

    void BuildIndex()
    {
        uint32 oldMSTime = getMSTime();
        rareIndex = Index();

        std::unordered_map<uint32, uint32> savedZones = LoadSavedZones();
        uint32 computed = 0;

        for (auto const& [spawnId, data] : sObjectMgr->GetAllCreatureData())
        {
            if (!IsOpenWorldMap(data.mapid) || !(data.phaseMask & PHASEMASK_NORMAL))
                continue;

            CreatureTemplate const* proto = sObjectMgr->GetCreatureTemplate(data.id);
            if (!proto || !IsRareRank(proto->rank) || proto->HasFlagsExtra(CREATURE_FLAG_EXTRA_TRIGGER)
                || LooksLikePlaceholder(proto->Name) || config.excludedEntries.count(proto->Entry))
                continue;

            RareSpawn spawn;
            spawn.spawnId = spawnId;
            spawn.entry = proto->Entry;
            spawn.map = data.mapid;
            spawn.x = data.posX;
            spawn.y = data.posY;
            spawn.gridId = Acore::ComputeGridCoord(data.posX, data.posY).GetId();
            spawn.poolId = data.poolId;
            spawn.spawnGroupId = data.spawnGroupId;
            spawn.respawnSeconds = data.spawntimesecs;

            if (auto saved = savedZones.find(uint32(spawnId)); saved != savedZones.end())
                spawn.zone = saved->second;
            else
            {
                // Safe here: OnStartup runs before the maps start updating.
                uint32 area = 0;
                sMapMgr->GetZoneAndAreaId(data.phaseMask, spawn.zone, area, data.mapid, data.posX, data.posY, data.posZ);
                ++computed;
            }

            ZoneName(spawn.zone);
            rareIndex.spawns.push_back(spawn);

            RareKind& kind = rareIndex.kinds[proto->Entry];
            kind.name = proto->Name;
            kind.minLevel = proto->minlevel;
            kind.maxLevel = proto->maxlevel;
            kind.elite = proto->rank == CREATURE_ELITE_RAREELITE;
        }

        LOG_INFO("server.loading", ">> mod-rare-tracker: {} rare spawns of {} rares in {} zones (worked out {} zones) in {} ms",
            rareIndex.spawns.size(), rareIndex.kinds.size(), rareIndex.zoneNames.size(), computed, GetMSTimeDiffToNow(oldMSTime));
    }

    // --- Snapshot -----------------------------------------------------------------------------

    std::string JsonString(std::string const& text)
    {
        std::string out = "\"";
        for (unsigned char c : text)
        {
            switch (c)
            {
                case '"':  out += "\\\""; break;
                case '\\': out += "\\\\"; break;
                case '\n': out += "\\n"; break;
                case '\r': out += "\\r"; break;
                case '\t': out += "\\t"; break;
                default:
                    if (c < 0x20)
                        out += Acore::StringFormat("\\u{:04x}", uint32(c));
                    else
                        out += char(c);
            }
        }
        return out + "\"";
    }

    // World map coordinates (0-100) for a spot in a zone, or false if the zone has no map of its own.
    bool ZoneCoordinates(uint32 zone, float worldX, float worldY, float& x, float& y)
    {
        x = worldX;
        y = worldY;
        Map2ZoneCoordinates(x, y, zone);
        return zone && x >= 0.0f && x <= 100.0f && y >= 0.0f && y <= 100.0f && !(x == worldX && y == worldY);
    }

    // Zones with no world map of their own: the open-air parts of dungeon entrances and Dalaran.
    // The game shows them on the map of the zone around them, so the rare map does too.
    std::unordered_map<uint32, std::vector<uint32>> const OUTDOOR_ZONES =
    {
        { 25,   { 51, 46 } },  // Blackrock Mountain: Searing Gorge, Burning Steppes
        { 1583, { 51, 46 } },  // Blackrock Spire
        { 1584, { 51, 46 } },  // Blackrock Depths
        { 1581, { 40 } },      // The Deadmines: Westfall
        { 718,  { 17 } },      // Wailing Caverns: The Barrens
        { 491,  { 17 } },      // Razorfen Kraul
        { 722,  { 17 } },      // Razorfen Downs
        { 719,  { 331 } },     // Blackfathom Deeps: Ashenvale
        { 209,  { 130 } },     // Shadowfang Keep: Silverpine Forest
        { 721,  { 1 } },       // Gnomeregan: Dun Morogh
        { 796,  { 85 } },      // Scarlet Monastery: Tirisfal Glades
        { 1337, { 3 } },       // Uldaman: Badlands
        { 1176, { 440 } },     // Zul'Farrak: Tanaris
        { 2100, { 405 } },     // Maraudon: Desolace
        { 1477, { 8 } },       // The Temple of Atal'Hakkar: Swamp of Sorrows
        { 2557, { 357 } },     // Dire Maul: Feralas
        { 2017, { 139 } },     // Stratholme: Eastern Plaguelands
        { 2057, { 28 } },      // Scholomance: Western Plaguelands
        { 4395, { 2817 } },    // Dalaran: Crystalsong Forest
    };

    // Map coordinates for a spot, moving it to the zone around it when its own zone has no map:
    // first the zones listed above, then any other zone on the same map whose map covers the spot
    // (nearest that map's centre). zone becomes the zone it's shown in.
    bool PlaceOnZoneMap(uint16 mapId, uint32& zone, float worldX, float worldY, float& x, float& y)
    {
        if (ZoneCoordinates(zone, worldX, worldY, x, y))
            return true;

        if (auto itr = OUTDOOR_ZONES.find(zone); itr != OUTDOOR_ZONES.end())
            for (uint32 outdoor : itr->second)
                if (ZoneCoordinates(outdoor, worldX, worldY, x, y))
                {
                    zone = outdoor;
                    return true;
                }

        uint32 best = 0;
        float bestDistance = 0.0f;
        for (AreaTableEntry const* area : sAreaTableStore)
        {
            if (area->zone || area->mapid != mapId || area->ID == zone)
                continue;

            float ax;
            float ay;
            if (!ZoneCoordinates(area->ID, worldX, worldY, ax, ay))
                continue;

            float distance = (ax - 50.0f) * (ax - 50.0f) + (ay - 50.0f) * (ay - 50.0f);
            if (!best || distance < bestDistance)
            {
                best = area->ID;
                bestDistance = distance;
                x = ax;
                y = ay;
            }
        }

        if (!best)
            return false;

        zone = best;
        return true;
    }

    // Runs on the world thread after the maps have finished updating, so map state is stable.
    std::string BuildSnapshot(int64 now)
    {
        std::ostringstream json;
        std::map<uint32, std::string const*> zones; // every zone the list mentions
        bool first = true;
        uint32 up = 0;

        for (RareSpawn const& spawn : rareIndex.spawns)
        {
            Map* map = sMapMgr->FindBaseNonInstanceMap(spawn.map);
            if (!map)
                continue;

            // Only spawns the server would load right now, checked the way GridObjectLoader does:
            // event spawns are added to and removed from the grid lists as their events start and
            // stop, spawn groups can be switched off per map, and pools pick one spot.
            CellObjectGuids const& grid = sObjectMgr->GetGridObjectGuids(spawn.map, 0, spawn.gridId);
            if (!grid.creatures.count(spawn.spawnId))
                continue;
            if (!map->IsSpawnGroupActive(spawn.spawnGroupId))
                continue;
            if (spawn.poolId && !map->GetPoolData().IsSpawnedObject<Creature>(spawn.spawnId))
                continue;

            Creature* live = nullptr;
            auto range = map->GetCreatureBySpawnIdStore().equal_range(spawn.spawnId);
            for (auto itr = range.first; itr != range.second; ++itr)
            {
                Creature* creature = itr->second;
                if (!creature || !creature->IsInWorld())
                    continue;
                if (!live || (creature->IsAlive() && !live->IsAlive()))
                    live = creature;
            }

            bool alive;
            int64 respawnAt = 0;
            float worldX = spawn.x;
            float worldY = spawn.y;
            uint32 zone = spawn.zone;

            if (live)
            {
                alive = live->IsAlive();
                if (alive)
                {
                    worldX = live->GetPositionX();
                    worldY = live->GetPositionY();
                    if (uint32 liveZone = live->GetZoneId())
                        zone = liveZone;
                }
                else
                    respawnAt = int64(live->GetRespawnTimeEx());
            }
            else
            {
                respawnAt = int64(map->GetCreatureRespawnTime(spawn.spawnId));
                alive = respawnAt <= now;
            }

            if (alive)
            {
                respawnAt = 0;
                ++up;
            }

            RareKind const& kind = rareIndex.kinds[spawn.entry];

            // A rare at a dungeon entrance or in Dalaran goes on the surrounding zone's map, and
            // "area" keeps the place it's really in.
            uint32 area = zone;
            float x;
            float y;
            bool placed = PlaceOnZoneMap(spawn.map, zone, worldX, worldY, x, y);
            zones.emplace(zone, &ZoneName(zone));

            json << (first ? "" : ",") << "{\"spawn\":" << spawn.spawnId << ",\"entry\":" << spawn.entry
                 << ",\"name\":" << JsonString(kind.name)
                 << ",\"minLevel\":" << uint32(kind.minLevel) << ",\"maxLevel\":" << uint32(kind.maxLevel)
                 << ",\"elite\":" << (kind.elite ? "true" : "false")
                 << ",\"map\":" << spawn.map << ",\"zone\":" << zone;
            if (area != zone)
            {
                zones.emplace(area, &ZoneName(area));
                json << ",\"area\":" << area;
            }
            json << ",\"wx\":" << Acore::StringFormat("{:.1f}", worldX) << ",\"wy\":" << Acore::StringFormat("{:.1f}", worldY);

            if (placed)
                json << ",\"x\":" << Acore::StringFormat("{:.2f}", x) << ",\"y\":" << Acore::StringFormat("{:.2f}", y);
            else
                json << ",\"x\":null,\"y\":null";

            json << ",\"state\":\"" << (alive ? "up" : "dead") << "\"";
            if (!alive && respawnAt > now)
                json << ",\"respawnAt\":" << respawnAt;
            json << ",\"respawnSeconds\":" << spawn.respawnSeconds;

            if (live)
            {
                json << ",\"live\":true";
                if (alive)
                    json << ",\"level\":" << uint32(live->GetLevel()) << ",\"hp\":" << uint32(live->GetHealthPct() + 0.5f)
                         << ",\"inCombat\":" << (live->IsInCombat() ? "true" : "false");
            }
            else
                json << ",\"live\":false";

            json << "}";
            first = false;
        }

        std::string out = Acore::StringFormat("{{\"generated\":{},\"refresh\":{},\"up\":{},\"zones\":{{", now, config.refreshSeconds, up);
        first = true;
        for (auto const& [zoneId, name] : zones)
        {
            out += Acore::StringFormat("{}\"{}\":{}", first ? "" : ",", zoneId, JsonString(*name));
            first = false;
        }

        return out + "},\"rares\":[" + json.str() + "]}";
    }

    std::unordered_set<uint32> ParseEntries(std::string const& text)
    {
        std::unordered_set<uint32> entries;
        std::string token;
        for (char c : text + ",")
        {
            if (c >= '0' && c <= '9')
                token += c;
            else if (!token.empty())
            {
                if (token.size() <= 9)
                    entries.insert(uint32(std::stoul(token)));
                token.clear();
            }
        }
        return entries;
    }
}

class RareTrackerWorldScript : public WorldScript
{
public:
    RareTrackerWorldScript() : WorldScript("RareTrackerWorldScript", {
        WORLDHOOK_ON_AFTER_CONFIG_LOAD, WORLDHOOK_ON_STARTUP, WORLDHOOK_ON_UPDATE, WORLDHOOK_ON_SHUTDOWN }) { }

    void OnAfterConfigLoad(bool reload) override
    {
        config.enabled = sConfigMgr->GetOption<bool>("RareTracker.Enable", true);
        config.botsIgnoreRares = sConfigMgr->GetOption<bool>("RareTracker.BotsIgnoreRares", true);
        config.refreshSeconds = std::max<uint32>(sConfigMgr->GetOption<uint32>("RareTracker.RefreshSeconds", 30), 5);
        config.idleSeconds = std::max<uint32>(sConfigMgr->GetOption<uint32>("RareTracker.IdleSeconds", 120), config.refreshSeconds);

        // Read on map threads and fixed at startup, so these need a restart to change.
        if (!reload)
        {
            config.excludedEntries = ParseEntries(sConfigMgr->GetOption<std::string>("RareTracker.ExcludeEntries", ""));
            config.httpEnabled = sConfigMgr->GetOption<bool>("RareTracker.Http.Enable", true);
            config.httpAddress = sConfigMgr->GetOption<std::string>("RareTracker.Http.BindAddress", "0.0.0.0");
            config.httpPort = uint16(sConfigMgr->GetOption<uint32>("RareTracker.Http.Port", 8095));
            config.allowOrigin = sConfigMgr->GetOption<std::string>("RareTracker.Http.AllowOrigin", "*");
        }
    }

    void OnStartup() override
    {
        if (!config.enabled)
            return;

        BuildIndex();

        if (config.httpEnabled)
            httpServer.Start(config.httpAddress, config.httpPort, config.allowOrigin);
    }

    void OnUpdate(uint32 /*diff*/) override
    {
        if (!config.enabled || !httpServer.IsRunning())
            return;

        int64 now = int64(GameTime::GetGameTime().count());
        int64 lastRequest = httpServer.LastRequestTime();
        if (!lastRequest || now - lastRequest > int64(config.idleSeconds))
            return; // nobody is looking

        if (now - lastBuild < int64(config.refreshSeconds))
            return;

        lastBuild = now;
        httpServer.Publish(BuildSnapshot(now));
    }

    void OnShutdown() override
    {
        httpServer.Stop();
    }
};

class RareTrackerUnitScript : public UnitScript
{
public:
    RareTrackerUnitScript() : UnitScript("RareTrackerUnitScript", true, { UNITHOOK_IF_NORMAL_REACTION }) { }

    // Free-roaming bots and open-world rares see each other as friendly: no targeting, no AoE,
    // no aggro.
    bool IfNormalReaction(Unit const* unit, Unit const* target, ReputationRank& repRank) override
    {
        if (!config.enabled || !config.botsIgnoreRares || !unit || !target)
            return true;

        if (!KeepBotOffRare(unit, target))
            return true;

        repRank = REP_FRIENDLY;
        return false;
    }

    // Backstop for damage that doesn't check reactions, or a fight that started while the bot was
    // still grouped with a player. The core calls this on every UnitScript.
    uint32 DealDamage(Unit* attacker, Unit* victim, uint32 damage, DamageEffectType /*damageType*/) override
    {
        if (config.enabled && config.botsIgnoreRares && attacker && victim && attacker != victim
            && IsOpenWorldRare(victim) && IsFreeRoamingBot(attacker))
            return 0;

        return damage;
    }
};

void AddRareTrackerScripts()
{
    new RareTrackerWorldScript();
    new RareTrackerUnitScript();
}

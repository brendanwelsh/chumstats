// Mock fixtures shaped to the carball DB schema (matches, match_player_stats,
// match_extras). All numbers chosen to look like one player's friend-group
// history — not a leaderboard, not a public dataset.

window.MOCK = (() => {
  const SELF = { name: "@ChumtheWaters", primary_id: "Steam|76561198042113006|0", platform: "Steam" };

  const PLAYERS = [
    SELF,
    { name: "@kelpdiver",     primary_id: "Steam|76561198091223781|0", platform: "Steam" },
    { name: "@oysterheist",   primary_id: "Epic|c41b9e2f8a1f4b22a7e2|0", platform: "Epic" },
    { name: "@sandbarry",     primary_id: "Steam|76561198122774401|0", platform: "Steam" },
    { name: "@tide_alex",     primary_id: "Epic|7e9d33b2a51c4d0a9f1c|0", platform: "Epic" },
    { name: "@mariniere",     primary_id: "Switch|0192847263|0",         platform: "Switch" },
    { name: "@brinepilot",    primary_id: "Steam|76561198205881337|0", platform: "Steam" },
    { name: "@halfcourt_h",   primary_id: "Steam|76561198044129055|0", platform: "Steam" },
    // recurring opponents
    { name: "rookwave",       primary_id: "Steam|76561198331122110|0", platform: "Steam" },
    { name: "fjord_seven",    primary_id: "Epic|aa3344bb55ccddee6677|0", platform: "Epic" },
    { name: "TURBOgroyne",    primary_id: "Steam|76561198399887766|0", platform: "Steam" },
    { name: "neptunia.gg",    primary_id: "Epic|bb44ddee33aacc7788991|0", platform: "Epic" },
    { name: "midnight-otter", primary_id: "Switch|0827361540|0",         platform: "Switch" },
    { name: "blue_collar_b",  primary_id: "Steam|76561198003302211|0", platform: "Steam" },
    { name: "littoral.zone",  primary_id: "Epic|99887766aabb44cc3322|0", platform: "Epic" },
    // a bot from the casual-vs-bots sessions
    { name: "Sundown",        primary_id: "Unknown|0|0", platform: "Unknown", is_bot: true },
    { name: "Foamer",         primary_id: "Unknown|0|0", platform: "Unknown", is_bot: true },
  ];

  const ARENAS = [
    "stadium_p", "stadium_day_p", "trainstation_night_p", "trainstation_p",
    "eurostadium_p", "eurostadium_night_p", "park_p", "park_night_p",
    "park_rainy_p", "wasteland_p", "chinastadium_p", "neotokyo_standard_p",
    "stadium_winter_p",
  ];
  const ARENA_NICE = {
    "stadium_p":            "DFH Stadium",
    "stadium_day_p":        "DFH Stadium (Day)",
    "trainstation_night_p": "Urban Central (Night)",
    "trainstation_p":       "Urban Central",
    "eurostadium_p":        "Mannfield",
    "eurostadium_night_p":  "Mannfield (Night)",
    "park_p":               "Beckwith Park",
    "park_night_p":         "Beckwith Park (Night)",
    "park_rainy_p":         "Beckwith Park (Stormy)",
    "wasteland_p":          "Wasteland",
    "chinastadium_p":       "Forbidden Temple",
    "neotokyo_standard_p":  "Neo Tokyo",
    "stadium_winter_p":     "Snowy Stadium (Snow Day)",
  };

  // Deterministic PRNG so the data is identical every reload
  // (otherwise scrubbing through history while iterating is annoying).
  function mulberry32(seed) {
    let a = seed >>> 0;
    return function () {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  const rng = mulberry32(20240517);
  const ri  = (a, b) => Math.floor(rng() * (b - a + 1)) + a;
  const rf  = (a, b) => rng() * (b - a) + a;
  const pick = (arr) => arr[Math.floor(rng() * arr.length)];
  const pickN = (arr, n) => {
    const c = arr.slice();
    const out = [];
    while (out.length < n && c.length) {
      out.push(c.splice(Math.floor(rng() * c.length), 1)[0]);
    }
    return out;
  };

  // Match generator. Produces a row in `matches` and N rows in
  // `match_player_stats`. Optional `extras` mirrors match_extras.
  function genMatch(idx, hoursAgo, mode) {
    const isOnline = mode !== "exhibition";
    const teamSize = mode === "1v1" ? 1 : mode === "2v2" ? 2 : 3;
    const hasBots = mode === "bots";

    // Roster: always include self, pick teammates + opponents.
    const friendPool = PLAYERS.filter(p => p !== SELF && !p.is_bot && p.name.startsWith("@"));
    const oppPool    = PLAYERS.filter(p => p !== SELF && !p.is_bot && !p.name.startsWith("@"));
    const teammates  = pickN(friendPool, teamSize - 1);
    const opponents  = hasBots
      ? pickN(PLAYERS.filter(p => p.is_bot), teamSize)
      : pickN(oppPool, teamSize);

    const blueRoster = [SELF, ...teammates];
    const orngRoster = opponents;

    // Score outcome: skewed slightly toward wins because friend group's a unit.
    const blueScore = ri(0, 7);
    let orngScore = ri(0, 7);
    if (blueScore === orngScore) orngScore = Math.max(0, orngScore - 1);
    const winnerTeam = blueScore > orngScore ? 0 : 1;
    const duration   = mode === "1v1" ? ri(180, 320) : ri(300, 540);
    const startedAt  = Date.now() / 1000 - hoursAgo * 3600;

    // Determine MVP — winning side's top scorer.
    const winRoster = winnerTeam === 0 ? blueRoster : orngRoster;
    const mvpPlayerIdx = 0; // we'll re-assign after stat gen by score

    // Stat generation per player.
    const teamScoresBlue = blueScore;
    const teamScoresOrng = orngScore;
    const playersData = [];

    function genPlayerStats(p, teamNum, teamScore, totalShots, hasSpectatorFields) {
      // Distribute goals among team to sum to teamScore. Self gets a slight edge.
      const isSelf = p === SELF;
      const goals    = Math.min(teamScore, isSelf ? ri(0, Math.min(3, teamScore)) : ri(0, teamScore));
      const shots    = goals + ri(1, 5);
      const assists  = ri(0, Math.max(0, teamScore - goals));
      const saves    = ri(0, 4);
      const demos    = ri(0, 3);
      const touches  = ri(40, 140);
      const score    = goals * 100 + assists * 50 + saves * 75 + shots * 10 + demos * 25 + ri(40, 220);

      const ticksTotal = hasSpectatorFields ? Math.floor(duration * 30) : 0;
      let onWall = 0, onGround = 0, inAir = 0, supersonic = 0, zeroBoost = 0, fullBoost = 0;
      let speedSum = 0, speedMax = 0, boostUsed = 0;
      if (ticksTotal) {
        onWall = Math.floor(ticksTotal * rf(0.02, 0.09));
        onGround = Math.floor(ticksTotal * rf(0.34, 0.52));
        inAir = ticksTotal - onWall - onGround;
        supersonic = Math.floor(ticksTotal * rf(0.18, 0.34));
        zeroBoost = Math.floor(ticksTotal * rf(0.04, 0.14));
        fullBoost = Math.floor(ticksTotal * rf(0.02, 0.10));
        const avgSpeed = rf(56, 78);
        speedSum = avgSpeed * ticksTotal;
        speedMax = rf(96, 132);
        boostUsed = rf(700, 1900);
      }

      return {
        name: p.name,
        primary_id: p.primary_id,
        team_num: teamNum,
        goals, assists, saves, shots, demos, touches, score,
        is_bot: !!p.is_bot,
        platform: p.platform,
        is_mvp: false, // computed after
        ticks_total: ticksTotal,
        ticks_on_wall: onWall, ticks_on_ground: onGround, ticks_in_air: inAir,
        ticks_supersonic: supersonic, ticks_zero_boost: zeroBoost, ticks_full_boost: fullBoost,
        speed_sum: speedSum, speed_max: speedMax, boost_used: boostUsed,
      };
    }

    blueRoster.forEach(p => playersData.push(genPlayerStats(p, 0, teamScoresBlue, 0, true)));
    // Opponents only have spectator fields ~5% of the time (replays).
    orngRoster.forEach(p => playersData.push(genPlayerStats(p, 1, teamScoresOrng, 0, false)));

    // Make per-team goal sums match team score.
    [0, 1].forEach(t => {
      const team = playersData.filter(p => p.team_num === t);
      const tScore = t === 0 ? teamScoresBlue : teamScoresOrng;
      const tGoals = team.reduce((s, p) => s + p.goals, 0);
      let diff = tScore - tGoals;
      // Adjust: bump or drop goals from the first scorer until equal
      while (diff > 0) {
        team[0].goals++; team[0].shots++; diff--;
      }
      while (diff < 0 && team.some(p => p.goals > 0)) {
        const candidate = team.find(p => p.goals > 0);
        candidate.goals--; diff++;
      }
    });

    // Pick MVP: top-scoring player on winning team.
    const winners = playersData.filter(p => p.team_num === winnerTeam && !p.is_bot)
      .sort((a, b) => b.score - a.score);
    if (winners[0]) winners[0].is_mvp = true;

    const team0Name = pickTeamName(mode, 0);
    const team1Name = pickTeamName(mode, 1);

    return {
      id: `match-${idx}`,
      started_at: startedAt,
      ended_at: startedAt + duration,
      arena: pick(ARENAS),
      is_online: isOnline,
      team0_score: teamScoresBlue,
      team1_score: teamScoresOrng,
      team0_name: team0Name,
      team1_name: team1Name,
      winner_team_num: winnerTeam,
      crossbar_hits: ri(0, 4),
      duration_seconds: duration,
      mode, // synthetic, not in real DB
      players: playersData,
    };
  }

  function pickTeamName(mode, side) {
    if (mode === "private") {
      const a = ["Spawnpoint Smackdown", "Foam Party FC", "Wallride Wizards", "BackboardBoys",
                 "Diagonal Demons", "Aerial Aficionados", "Dunktown Reps", "ZeroSecond Saviors"];
      return pick(a);
    }
    return side === 0 ? "Blue" : "Orange";
  }

  // Build a chronological log.
  const matches = [];
  let hAgo = 0.3;
  const modeCycle = ["2v2", "2v2", "3v3", "2v2", "1v1", "3v3", "2v2", "private",
                     "2v2", "3v3", "2v2", "bots", "3v3", "2v2", "exhibition"];
  for (let i = 0; i < 124; i++) {
    const mode = modeCycle[i % modeCycle.length];
    matches.push(genMatch(124 - i, hAgo, mode));
    hAgo += rf(0.2, 12);
  }
  matches.sort((a, b) => b.started_at - a.started_at);

  // Career aggregates for SELF (computed live in components; expose helpers here).
  function selfLines() {
    return matches.flatMap(m => m.players.filter(p => p.primary_id === SELF.primary_id).map(p => ({ ...p, match: m })));
  }

  function aggregateForPlayer(primaryId, name) {
    const lines = matches.flatMap(m => m.players
      .filter(p => primaryId ? p.primary_id === primaryId : p.name === name)
      .map(p => ({ ...p, match: m })));
    if (!lines.length) return null;
    const wins = lines.filter(l => l.team_num === l.match.winner_team_num).length;
    const matchCount = lines.length;
    const sum = (k) => lines.reduce((s, l) => s + (l[k] || 0), 0);
    const ticks = sum("ticks_total");
    const speedSum = sum("speed_sum");
    return {
      matches: matchCount,
      wins, losses: matchCount - wins,
      win_rate: wins / matchCount,
      mvp: lines.filter(l => l.is_mvp).length,
      goals: sum("goals"),
      assists: sum("assists"),
      saves: sum("saves"),
      shots: sum("shots"),
      demos: sum("demos"),
      touches: sum("touches"),
      score: sum("score"),
      ticks,
      avg_goals:   sum("goals")   / matchCount,
      avg_assists: sum("assists") / matchCount,
      avg_saves:   sum("saves")   / matchCount,
      avg_shots:   sum("shots")   / matchCount,
      avg_demos:   sum("demos")   / matchCount,
      avg_score:   sum("score")   / matchCount,
      avg_touches: sum("touches") / matchCount,
      shot_pct: sum("shots") ? sum("goals") / sum("shots") : 0,
      pct_supersonic: ticks ? sum("ticks_supersonic") / ticks : 0,
      pct_in_air:     ticks ? sum("ticks_in_air")     / ticks : 0,
      pct_on_wall:    ticks ? sum("ticks_on_wall")    / ticks : 0,
      pct_on_ground:  ticks ? sum("ticks_on_ground")  / ticks : 0,
      avg_speed:      ticks ? speedSum / ticks : 0,
      speed_max: Math.max(...lines.map(l => l.speed_max)),
      boost_used: sum("boost_used"),
      pct_zero:  ticks ? sum("ticks_zero_boost") / ticks : 0,
      pct_full:  ticks ? sum("ticks_full_boost") / ticks : 0,
      recent: lines.slice(0, 10), // already sorted by match desc
      lines,
      records: {
        max_goals: Math.max(...lines.map(l => l.goals)),
        max_assists: Math.max(...lines.map(l => l.assists)),
        max_saves: Math.max(...lines.map(l => l.saves)),
        max_shots: Math.max(...lines.map(l => l.shots)),
        max_demos: Math.max(...lines.map(l => l.demos)),
        max_score: Math.max(...lines.map(l => l.score)),
      },
    };
  }

  function arenaNice(slug) {
    return ARENA_NICE[slug] || slug.replace(/_/g, " ");
  }
  function modeLabel(mode, m) {
    if (mode === "exhibition") return "Exhibition";
    if (mode === "bots")       return "Casual vs Bots";
    if (mode === "private")    return "Private";
    return ({"1v1": "1v1 Duels", "2v2": "2v2 Doubles", "3v3": "3v3 Standard"}[mode]) || "Online";
  }
  function fmtClock(s) {
    const mm = Math.floor(s / 60), ss = Math.floor(s % 60);
    return `${mm}:${ss.toString().padStart(2,'0')}`;
  }
  function fmtRelTime(epoch) {
    const diff = (Date.now() / 1000) - epoch;
    if (diff < 60) return "just now";
    if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
    if (diff < 86400*7) return `${Math.floor(diff/86400)}d ago`;
    if (diff < 86400*30) return `${Math.floor(diff/86400/7)}w ago`;
    return new Date(epoch*1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  function fmtDate(epoch) {
    return new Date(epoch*1000).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit"
    });
  }

  return {
    SELF, PLAYERS, ARENAS, ARENA_NICE, matches,
    aggregateForPlayer, arenaNice, modeLabel, fmtClock, fmtRelTime, fmtDate,
  };
})();

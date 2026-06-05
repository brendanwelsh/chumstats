// Players directory + profile screens.

function PlayersDirectory({ go }) {
  const SELF = window.MOCK.SELF;
  const matches = window.MOCK.matches;

  const [showBots, setShowBots] = useState(true);
  const [search, setSearch]     = useState("");
  const [sort, setSort]         = useState("matches"); // matches | wins | goals | name

  // Aggregate by (name, primary_id)
  const directory = useMemo(() => {
    const byKey = new Map();
    matches.forEach(m => {
      m.players.forEach(p => {
        const key = p.is_bot ? `name:${p.name}` : p.primary_id;
        let r = byKey.get(key);
        if (!r) {
          r = { name: p.name, primary_id: p.primary_id, is_bot: p.is_bot, platform: p.platform,
                matches: 0, wins: 0, goals: 0, assists: 0, saves: 0, shots: 0, demos: 0,
                wasTeammate: false, wasOpponent: false, isSelf: p.primary_id === SELF.primary_id };
          byKey.set(key, r);
        }
        r.matches++;
        if (p.team_num === m.winner_team_num) r.wins++;
        r.goals += p.goals; r.assists += p.assists; r.saves += p.saves;
        r.shots += p.shots; r.demos += p.demos;
        if (p.primary_id !== SELF.primary_id) {
          const selfInMatch = m.players.find(x => x.primary_id === SELF.primary_id);
          if (selfInMatch) {
            if (p.team_num === selfInMatch.team_num) r.wasTeammate = true;
            else r.wasOpponent = true;
          }
        }
      });
    });
    return Array.from(byKey.values());
  }, [matches]);

  const filtered = useMemo(() => {
    let list = directory.filter(r => showBots || !r.is_bot);
    if (search) {
      const q = search.toLowerCase();
      list = list.filter(r => r.name.toLowerCase().includes(q));
    }
    list.sort((a, b) => {
      if (sort === "name")    return a.name.localeCompare(b.name);
      if (sort === "wins")    return b.wins - a.wins;
      if (sort === "goals")   return b.goals - a.goals;
      return b.matches - a.matches;
    });
    return list;
  }, [directory, showBots, search, sort]);

  // Stats summary
  const total = filtered.length;
  const teammates = filtered.filter(r => r.wasTeammate && !r.isSelf).length;
  const opps      = filtered.filter(r => r.wasOpponent && !r.isSelf).length;

  return (
    <div>
      <PageHead
        title="Players"
        sub={`${total} unique players recorded · ${teammates} teammates, ${opps} opponents`}
      />

      <div className="toolbar">
        <div className="seg">
          {[
            ["matches", "Most played"],
            ["wins",    "Most wins"],
            ["goals",   "Most goals"],
            ["name",    "A–Z"],
          ].map(([k, l]) => (
            <button key={k} className={sort === k ? "active" : ""} onClick={() => setSort(k)}>{l}</button>
          ))}
        </div>
        <div className="search-box">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
          <input placeholder="Filter players…" value={search} onChange={e => setSearch(e.target.value)} />
        </div>
        <label style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--text-dim)", cursor: "pointer", marginLeft: "auto" }}>
          <input type="checkbox" checked={showBots} onChange={e => setShowBots(e.target.checked)} />
          Show bots
        </label>
      </div>

      <div className="card" style={{ padding: 0, overflow: "hidden" }}>
        <table>
          <thead>
            <tr>
              <th>Player</th>
              <th>Platform</th>
              <th>Relation</th>
              <th className="num">Matches</th>
              <th className="num">W-L</th>
              <th className="num">Win %</th>
              <th className="num">Goals</th>
              <th className="num">Assists</th>
              <th className="num">Saves</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((r, i) => {
              const winPct = r.matches ? (r.wins / r.matches) * 100 : 0;
              const relations = [];
              if (r.isSelf) relations.push("you");
              if (r.wasTeammate) relations.push("teammate");
              if (r.wasOpponent) relations.push("opponent");
              return (
                <tr key={i} className="row click" onClick={() => go(r.is_bot ? "" : `player/${encodeURIComponent(r.name)}`)}>
                  <td>
                    <PlayerLink name={r.name} primary_id={r.primary_id} is_bot={r.is_bot} go={go} />
                    {r.isSelf && <span className="you-marker">YOU</span>}
                    {r.is_bot && <Chip kind="bot" >BOT</Chip>}
                  </td>
                  <td className="dim">{r.platform}</td>
                  <td className="dim" style={{ fontSize: 12 }}>{relations.join(" · ") || "—"}</td>
                  <td className="num tnum">{r.matches}</td>
                  <td className="num tnum"><b>{r.wins}</b><span className="dim">-{r.matches - r.wins}</span></td>
                  <td className="num tnum dim">{winPct.toFixed(0)}%</td>
                  <td className="num tnum">{r.goals}</td>
                  <td className="num tnum">{r.assists}</td>
                  <td className="num tnum">{r.saves}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ----- single player profile (mirror of dashboard) -----

function PlayerProfile({ name, go }) {
  const SELF = window.MOCK.SELF;
  const playerMatches = window.MOCK.matches.filter(m =>
    m.players.some(p => p.name === name)
  );
  if (!playerMatches.length) {
    return <div className="empty">No matches recorded for {name}.</div>;
  }
  const sample = playerMatches[0].players.find(p => p.name === name);
  const agg = window.MOCK.aggregateForPlayer(sample.primary_id, name);
  if (!agg) return <div className="empty">No matches for {name}.</div>;

  const isSelf = sample.primary_id === SELF.primary_id;

  // Build co-play / vs records against the owner
  const vsSelf = playerMatches.reduce((acc, m) => {
    const them = m.players.find(p => p.name === name);
    const meIn = m.players.find(p => p.primary_id === SELF.primary_id);
    if (!meIn) return acc;
    if (them.primary_id === SELF.primary_id) return acc;
    if (them.team_num === meIn.team_num) {
      acc.teammate.matches++;
      if (them.team_num === m.winner_team_num) acc.teammate.wins++;
    } else {
      acc.opponent.matches++;
      if (meIn.team_num === m.winner_team_num) acc.opponent.youWon++;
    }
    return acc;
  }, { teammate: { matches: 0, wins: 0 }, opponent: { matches: 0, youWon: 0 } });

  // Radar peaks: scaled to this player's own single-match record.
  const peaks = {
    g: agg.records.max_goals, a: agg.records.max_assists,
    sv: agg.records.max_saves, sh: agg.records.max_shots, d: agg.records.max_demos,
  };
  const radarValues = [
    { label: "Goals",   value: agg.avg_goals,   max: peaks.g  || 1 },
    { label: "Shots",   value: agg.avg_shots,   max: peaks.sh || 1 },
    { label: "Demos",   value: agg.avg_demos,   max: peaks.d  || 1 },
    { label: "Saves",   value: agg.avg_saves,   max: peaks.sv || 1 },
    { label: "Assists", value: agg.avg_assists, max: peaks.a  || 1 },
  ];

  const lastTen = agg.recent.map(r => r.team_num === r.match.winner_team_num);
  const winsRecent = lastTen.filter(Boolean).length;
  const initials = name.replace(/^@/, "").slice(0, 2).toUpperCase();
  const hasAdv = agg.ticks > 1000;

  return (
    <div>
      <div className="breadcrumb">
        <a onClick={() => go("players")}>← Players</a>
      </div>

      <header className="player-head">
        <div className={"avatar" + (isSelf ? " self" : "")}>
          {initials}
          <span className="platform-pip" title={sample.platform}>{sample.platform[0]}</span>
        </div>
        <div className="name-line">
          <h1>{name}</h1>
          <div className="meta">
            <span>{sample.platform}</span>
            <span>·</span>
            <span>{agg.matches} matches recorded</span>
            <span>·</span>
            <span>First seen {window.MOCK.fmtRelTime(playerMatches[playerMatches.length-1].started_at)}</span>
            {isSelf && <Chip kind="mvp">Owner</Chip>}
            {!isSelf && vsSelf.teammate.matches > 0 && <Chip>teammate</Chip>}
            {!isSelf && vsSelf.opponent.matches > 0 && <Chip>opponent</Chip>}
          </div>
        </div>
        <div className="quick">
          <div className="quick-stat">
            <div className="v tnum">{agg.wins}-{agg.losses}</div>
            <div className="l">Career</div>
          </div>
          <div className="quick-stat">
            <div className="v tnum">{(agg.win_rate * 100).toFixed(0)}%</div>
            <div className="l">Win rate</div>
          </div>
          <div className="quick-stat">
            <div className="v tnum">{agg.mvp}</div>
            <div className="l">MVPs</div>
          </div>
        </div>
      </header>

      <div className="kpi-row">
        <KPI label="Goals / match"   value={agg.avg_goals.toFixed(2)}   foot={<span>Lifetime <b className="tnum">{agg.goals}</b></span>} />
        <KPI label="Assists / match" value={agg.avg_assists.toFixed(2)} foot={<span>Lifetime <b className="tnum">{agg.assists}</b></span>} />
        <KPI label="Saves / match"   value={agg.avg_saves.toFixed(2)}   foot={<span>Lifetime <b className="tnum">{agg.saves}</b></span>} />
        <KPI label="Shooting %"      value={`${(agg.shot_pct * 100).toFixed(0)}%`} foot={<span>{agg.goals} / {agg.shots}</span>} />
      </div>

      <div className="dash-grid">
        <div>
          <div className="card card-pad-lg">
            <div className="section-title">
              <span>Playstyle</span>
              <span className="dim">Per-match averages, scaled to {name}'s record</span>
            </div>
            <div className="radar-block">
              <Radar values={radarValues} size={260} />
              <div className="legend">
                {radarValues.map((v, i) => (
                  <div className="row" key={i}>
                    <span className="lbl">{v.label}</span>
                    <span className="val tnum">{v.value.toFixed(2)}</span>
                    <span className="max">/ {v.max} record</span>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {hasAdv && (
            <div className="card" style={{ marginTop: 14 }}>
              <div className="section-title">
                <span>Movement</span>
                <span className="dim">
                  {isSelf ? "Full coverage" : "Partial — opponent ticks missing per the spectator-fields rule"}
                </span>
              </div>
              <Stack segments={[
                { label: "On ground", pct: agg.pct_on_ground, color: "var(--text-faint)" },
                { label: "In air",    pct: agg.pct_in_air,    color: "var(--team-blue)" },
                { label: "On wall",   pct: agg.pct_on_wall,   color: "var(--accent)" },
              ]} />
              <div className="divider" />
              <div className="statgrid">
                <StatRow k="Supersonic"        v={(agg.pct_supersonic * 100).toFixed(1) + "%"} c="of tracked time" />
                <StatRow k="Avg speed"         v={agg.avg_speed.toFixed(1)} c={`max ${agg.speed_max.toFixed(0)}`} />
                <StatRow k="Boost / match"     v={(agg.boost_used / agg.matches).toFixed(0)} c="" />
              </div>
            </div>
          )}
        </div>

        <div>
          <div className="card">
            <div className="section-title"><span>Recent form</span></div>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14 }}>
              <FormDots results={lastTen} max={10} />
              <span className="tnum" style={{ fontWeight: 800, fontSize: 22 }}>
                {winsRecent}<span className="dim" style={{ fontSize: 14 }}>-{lastTen.length - winsRecent}</span>
              </span>
            </div>
            <div className="dim" style={{ fontSize: 11 }}>Last {lastTen.length} matches</div>
          </div>

          {!isSelf && (
            <div className="card" style={{ marginTop: 14 }}>
              <div className="section-title"><span>vs you</span></div>
              <div className="statgrid">
                <StatRow k="Times as teammate" v={vsSelf.teammate.matches}
                         c={vsSelf.teammate.matches ? `${vsSelf.teammate.wins}W as duo` : ""} />
                <StatRow k="Times as opponent" v={vsSelf.opponent.matches}
                         c={vsSelf.opponent.matches ? `you led ${vsSelf.opponent.youWon}-${vsSelf.opponent.matches - vsSelf.opponent.youWon}` : ""} />
              </div>
            </div>
          )}

          <div className="card" style={{ marginTop: 14 }}>
            <div className="section-title"><span>Single-match records</span></div>
            <div className="statgrid">
              <StatRow k="Goals"   v={agg.records.max_goals}   c="" />
              <StatRow k="Assists" v={agg.records.max_assists} c="" />
              <StatRow k="Saves"   v={agg.records.max_saves}   c="" />
              <StatRow k="Shots"   v={agg.records.max_shots}   c="" />
              <StatRow k="Demos"   v={agg.records.max_demos}   c="" />
              <StatRow k="Score"   v={agg.records.max_score}   c="" />
            </div>
          </div>
        </div>
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <div className="section-title">
          <span>All matches</span>
          <span className="dim tnum">{agg.matches}</span>
        </div>
        <table className="tight">
          <tbody>
            {agg.lines.slice(0, 20).map((l, i) => {
              const m = l.match;
              const won = l.team_num === m.winner_team_num;
              return (
                <tr key={i} className="row click" onClick={() => go(`match/${m.id}`)}>
                  <td style={{ width: 36 }}><ResultBadge won={won} /></td>
                  <td className="dim tnum" style={{ width: 80 }}>{window.MOCK.fmtRelTime(m.started_at)}</td>
                  <td>
                    <span className="tnum"><b>{m.team0_score}</b> – <b>{m.team1_score}</b></span>
                    <span className="dim" style={{ marginLeft: 8 }}>{window.MOCK.arenaNice(m.arena)}</span>
                  </td>
                  <td className="num tnum" style={{ fontSize: 12 }}>{l.goals}G {l.assists}A {l.saves}Sv</td>
                  <td>{l.is_mvp && <Chip kind="mvp">MVP</Chip>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

Object.assign(window, { PlayersDirectory, PlayerProfile });

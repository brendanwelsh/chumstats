// Me / Dashboard screen — career stats for the configured owner.

function Dashboard({ go, tweaks }) {
  const SELF = window.MOCK.SELF;
  const agg = window.MOCK.aggregateForPlayer(SELF.primary_id, SELF.name);
  if (!agg) return <div className="empty">No matches recorded yet.</div>;

  const winPctNow  = agg.win_rate;
  const lastTen    = agg.recent.map(r => r.team_num === r.match.winner_team_num);
  const winsRecent = lastTen.filter(Boolean).length;

  // Last-10 vs lifetime delta — gives the dashboard movement
  const lifetimeRate = agg.win_rate;
  const recentRate   = winsRecent / Math.max(1, lastTen.length);
  const winRateDelta = recentRate - lifetimeRate;

  // Sparkbar data — last 20 matches' personal scores
  const last20 = agg.lines.slice(0, 20).reverse();
  const scoreBars = last20.map(l => l.score);
  const winFlags  = last20.map(l => l.team_num === l.match.winner_team_num);

  // Radar peaks scaled to single-match record across the DB.
  const peaks = {
    g:  Math.max(1, ...agg.lines.map(l => l.goals)),
    a:  Math.max(1, ...agg.lines.map(l => l.assists)),
    sv: Math.max(1, ...agg.lines.map(l => l.saves)),
    sh: Math.max(1, ...agg.lines.map(l => l.shots)),
    d:  Math.max(1, ...agg.lines.map(l => l.demos)),
  };
  const radarValues = [
    { label: "Goals",   value: agg.avg_goals,   max: peaks.g  },
    { label: "Shots",   value: agg.avg_shots,   max: peaks.sh },
    { label: "Demos",   value: agg.avg_demos,   max: peaks.d  },
    { label: "Saves",   value: agg.avg_saves,   max: peaks.sv },
    { label: "Assists", value: agg.avg_assists, max: peaks.a  },
  ];

  const recentMatches = agg.recent.map(l => l.match).slice(0, 6);

  return (
    <div>
      <PageHead
        title={SELF.name}
        sub={<><span>Career dashboard · {agg.matches} matches recorded</span></>}
        right={
          <div className="quick">
            <Chip>{SELF.platform}</Chip>
            <Chip kind="mvp">Owner</Chip>
          </div>
        }
      />

      {/* KPI row */}
      <div className="kpi-row">
        <KPI primary
             label="Career"
             value={`${agg.wins}-${agg.losses}`}
             foot={<><span>Win rate</span> <b className="tnum" style={{ color: "var(--text)" }}>{(winPctNow * 100).toFixed(1)}%</b></>} />
        <KPI label="MVPs"
             value={agg.mvp}
             foot={<><span>{((agg.mvp / agg.matches) * 100).toFixed(0)}% of matches</span></>} />
        <KPI label="Goals / match"
             value={agg.avg_goals.toFixed(2)}
             foot={<><span>Lifetime</span> <b className="tnum dim">{agg.goals}</b></>} />
        <KPI label="Shooting %"
             value={`${(agg.shot_pct * 100).toFixed(0)}%`}
             foot={<><span>{agg.goals} of {agg.shots} shots</span></>} />
        <KPI label="Last 10"
             value={`${winsRecent}-${lastTen.length - winsRecent}`}
             foot={
               <>
                 <span className={"delta " + (winRateDelta >= 0 ? "up" : "down")}>
                   {winRateDelta >= 0 ? "▲" : "▼"} {Math.abs(winRateDelta * 100).toFixed(1)}%
                 </span>
                 <span>vs career</span>
               </>
             } />
      </div>

      <div className="dash-grid">
        {/* LEFT: radar + sparkbar + form */}
        <div>
          <div className="card card-pad-lg">
            <div className="section-title">
              <span>Playstyle</span>
              <span className="extras dim">Per-match averages, scaled to your single-match record</span>
            </div>
            <div className="radar-block">
              <Radar values={radarValues} size={280} />
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

          <div className="card" style={{ marginTop: 14 }}>
            <div className="section-title">
              <span>Score per match · last 20</span>
              <span className="dim tnum">peak {Math.max(...scoreBars)}</span>
            </div>
            <Sparkbar values={scoreBars} highlight={winFlags} />
            <div className="dim" style={{ fontSize: 11, marginTop: 6 }}>
              Orange = wins · Grey = losses
            </div>
          </div>

          <div className="card" style={{ marginTop: 14 }}>
            <div className="section-title">
              <span>Movement / Boost · lifetime</span>
              <span className="dim">From {Math.round(agg.ticks/30/60).toLocaleString()}m of tracked play</span>
            </div>
            <div style={{ marginBottom: 14 }}>
              <div className="eyebrow" style={{ margin: "0 0 8px" }}>Where you are on the field</div>
              <Stack segments={[
                { label: "On ground", pct: agg.pct_on_ground, color: "var(--text-faint)" },
                { label: "In air",    pct: agg.pct_in_air,    color: "var(--team-blue)" },
                { label: "On wall",   pct: agg.pct_on_wall,   color: "var(--accent)" },
              ]} />
            </div>
            <div>
              <div className="eyebrow" style={{ margin: "0 0 8px" }}>How you boost</div>
              <Stack segments={[
                { label: "At 0",    pct: agg.pct_zero,          color: "var(--bad)" },
                { label: "Mid",     pct: 1 - agg.pct_zero - agg.pct_full, color: "var(--text-faint)" },
                { label: "At 100",  pct: agg.pct_full,          color: "var(--good)" },
              ]} />
            </div>
            <div className="divider" />
            <div className="statgrid">
              <StatRow k="Supersonic" v={(agg.pct_supersonic * 100).toFixed(1) + "%"}  c="of tracked time" />
              <StatRow k="Avg speed"  v={agg.avg_speed.toFixed(1)}                     c={`max ${agg.speed_max.toFixed(0)}`} />
              <StatRow k="Boost used / match" v={(agg.boost_used / agg.matches).toFixed(0)} c={`~${((agg.boost_used / agg.matches) / 100).toFixed(1)} full tanks`} />
            </div>
          </div>
        </div>

        {/* RIGHT: recent form + records */}
        <div>
          <div className="card">
            <div className="section-title">
              <span>Recent form</span>
              <span className="dim">Newest →</span>
            </div>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14 }}>
              <FormDots results={lastTen} max={10} />
              <span className="tnum" style={{ fontWeight: 800, fontSize: 22, letterSpacing: "-0.02em" }}>
                {winsRecent}<span className="dim" style={{ fontSize: 14, fontWeight: 600 }}>-{lastTen.length - winsRecent}</span>
              </span>
            </div>
            <div className="statgrid">
              <StatRow k="Avg goals · last 10"   v={(agg.recent.reduce((s,r)=>s+r.goals,0)/agg.recent.length).toFixed(2)} c="" />
              <StatRow k="Avg assists · last 10" v={(agg.recent.reduce((s,r)=>s+r.assists,0)/agg.recent.length).toFixed(2)} c="" />
              <StatRow k="Avg saves · last 10"   v={(agg.recent.reduce((s,r)=>s+r.saves,0)/agg.recent.length).toFixed(2)} c="" />
              <StatRow k="Avg shots · last 10"   v={(agg.recent.reduce((s,r)=>s+r.shots,0)/agg.recent.length).toFixed(2)} c="" />
            </div>
          </div>

          <div className="card" style={{ marginTop: 14 }}>
            <div className="section-title">
              <span>Single-match records</span>
            </div>
            <div className="statgrid">
              <StatRow k="Goals in a match"   v={agg.records.max_goals}   c="" />
              <StatRow k="Assists in a match" v={agg.records.max_assists} c="" />
              <StatRow k="Saves in a match"   v={agg.records.max_saves}   c="" />
              <StatRow k="Shots in a match"   v={agg.records.max_shots}   c="" />
              <StatRow k="Demos in a match"   v={agg.records.max_demos}   c="" />
              <StatRow k="Score in a match"   v={agg.records.max_score}   c="" />
            </div>
          </div>

          <div className="card" style={{ marginTop: 14 }}>
            <div className="section-title">
              <span>Recent matches</span>
              <a className="see-all" onClick={() => go("matches")}>View all →</a>
            </div>
            <table className="tight">
              <tbody>
                {recentMatches.map(m => {
                  const me = m.players.find(p => p.primary_id === SELF.primary_id);
                  const won = me.team_num === m.winner_team_num;
                  return (
                    <tr key={m.id} className="row click" onClick={() => go(`match/${m.id}`)}>
                      <td style={{ width: 36 }}><ResultBadge won={won} /></td>
                      <td className="dim" style={{ width: 78 }}>{window.MOCK.fmtRelTime(m.started_at)}</td>
                      <td>
                        <span className={"tnum " + (won ? "" : "dim")}>
                          <b>{m.team0_score}</b> – <b>{m.team1_score}</b>
                        </span>
                        <span className="dim" style={{ marginLeft: 8, fontSize: 12 }}>{window.MOCK.arenaNice(m.arena)}</span>
                      </td>
                      <td className="num tnum" style={{ fontSize: 12 }}>{me.goals}G {me.assists}A {me.saves}Sv</td>
                      <td>{me.is_mvp && <Chip kind="mvp">MVP</Chip>}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div className="note" style={{ marginTop: 14 }}>
        <b>Local data, period.</b> Everything on this page came from your machine's
        <code style={{ background: "var(--bg)", padding: "1px 5px", borderRadius: 4, margin: "0 4px" }}>data/ballshark.db</code>
        — no ballchasing, no tracker.gg, no Psyonix REST.
      </div>
    </div>
  );
}

window.Dashboard = Dashboard;

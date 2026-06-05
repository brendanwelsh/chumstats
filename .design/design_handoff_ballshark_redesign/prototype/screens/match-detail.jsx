// Match detail screen — hero scoreboard banner, two team rosters, per-player radars.

function MatchDetail({ matchId, go }) {
  const m = window.MOCK.matches.find(x => x.id === matchId);
  if (!m) return <div className="empty">Match not found.</div>;

  const SELF = window.MOCK.SELF;
  const me = m.players.find(p => p.primary_id === SELF.primary_id);
  const winnerIs0 = m.winner_team_num === 0;
  const teamBlue   = m.players.filter(p => p.team_num === 0).sort((a, b) => b.score - a.score);
  const teamOrng   = m.players.filter(p => p.team_num === 1).sort((a, b) => b.score - a.score);

  // Peak values across this match for per-player radar scaling.
  const peak = (k) => Math.max(1, ...m.players.map(p => p[k]));
  const peaks = { g: peak("goals"), a: peak("assists"), sv: peak("saves"), sh: peak("shots"), d: peak("demos") };

  const teamTotals = (team) => ({
    score:   team.reduce((s, p) => s + p.score, 0),
    goals:   team.reduce((s, p) => s + p.goals, 0),
    assists: team.reduce((s, p) => s + p.assists, 0),
    saves:   team.reduce((s, p) => s + p.saves, 0),
    shots:   team.reduce((s, p) => s + p.shots, 0),
    demos:   team.reduce((s, p) => s + p.demos, 0),
  });

  function Roster({ team, teamNum, name, score, isWinner }) {
    const teamCls = teamNum === 0 ? "team-blue" : "team-orng";
    const totals  = teamTotals(team);
    return (
      <div className={"roster-card " + teamCls}>
        <div className="roster-head">
          <div className="roster-team">
            <span className="roster-stripe"></span>
            <span>{name}</span>
            {isWinner && <Chip kind="win">Winner</Chip>}
          </div>
          <span className="roster-score tnum">{score}</span>
        </div>
        <table>
          <thead>
            <tr>
              <th>Player</th>
              <th className="num">Score</th>
              <th className="num">G</th>
              <th className="num">A</th>
              <th className="num">Sv</th>
              <th className="num">Sh</th>
              <th className="num">D</th>
              <th className="num">Touch</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {team.map((p, i) => {
              const isSelf = p.primary_id === SELF.primary_id;
              const hasAdv = p.ticks_total >= 200;
              return (
                <tr key={i}>
                  <td className="player-cell">
                    <PlayerLink name={p.name} primary_id={p.primary_id} is_bot={p.is_bot} go={go} />
                    {isSelf && <span className="you-marker">YOU</span>}
                    {p.is_bot && <Chip kind="bot">BOT</Chip>}
                    {hasAdv && (
                      <div className="meta-line">
                        <span>{p.platform}</span>
                        <span>·</span>
                        <span>supersonic <span className="super">{(p.ticks_supersonic / p.ticks_total * 100).toFixed(0)}%</span></span>
                        <span>·</span>
                        <span>air {((p.ticks_in_air / p.ticks_total) * 100).toFixed(0)}%</span>
                        <span>·</span>
                        <span>boost {p.boost_used.toFixed(0)}</span>
                      </div>
                    )}
                    {!hasAdv && (
                      <div className="meta-line">
                        <span>{p.platform}</span>
                        <span>·</span>
                        <em className="dim">spectator-only fields unavailable for opponents</em>
                      </div>
                    )}
                  </td>
                  <td className="num tnum"><b>{p.score}</b></td>
                  <td className="num tnum">{p.goals}</td>
                  <td className="num tnum">{p.assists}</td>
                  <td className="num tnum">{p.saves}</td>
                  <td className="num tnum">{p.shots}</td>
                  <td className="num tnum">{p.demos}</td>
                  <td className="num tnum">{p.touches}</td>
                  <td>{p.is_mvp && <Chip kind="mvp">MVP</Chip>}</td>
                </tr>
              );
            })}
            <tr className="total-row">
              <td>Team total</td>
              <td className="num tnum">{totals.score}</td>
              <td className="num tnum">{totals.goals}</td>
              <td className="num tnum">{totals.assists}</td>
              <td className="num tnum">{totals.saves}</td>
              <td className="num tnum">{totals.shots}</td>
              <td className="num tnum">{totals.demos}</td>
              <td></td>
              <td></td>
            </tr>
          </tbody>
        </table>
      </div>
    );
  }

  function PlayerRadarCard({ p }) {
    const teamCls = p.team_num === 0 ? "team-blue" : "team-orng";
    const color   = p.team_num === 0 ? "var(--team-blue)" : "var(--team-orng)";
    const values = [
      { label: "G",  value: p.goals,   max: peaks.g  },
      { label: "Sh", value: p.shots,   max: peaks.sh },
      { label: "D",  value: p.demos,   max: peaks.d  },
      { label: "Sv", value: p.saves,   max: peaks.sv },
      { label: "A",  value: p.assists, max: peaks.a  },
    ];
    return (
      <div className={"radar-card " + teamCls}>
        <div className="rc-head">
          <div>
            <PlayerLink name={p.name} primary_id={p.primary_id} is_bot={p.is_bot} go={go} />
            {p.is_mvp && <span className="chip mvp" style={{ marginLeft: 6 }}>MVP</span>}
            {p.primary_id === SELF.primary_id && <span className="you-marker" style={{ marginLeft: 6 }}>YOU</span>}
          </div>
          <span className="rc-score">{p.score}</span>
        </div>
        <Radar values={values} size={200} color={color} axisFont={9} />
      </div>
    );
  }

  return (
    <div>
      <div className="breadcrumb">
        <a onClick={() => go("matches")}>← Matches</a> <span style={{ margin: "0 6px" }}>/</span> <span className="tnum">{m.id}</span>
      </div>

      {/* Hero scoreboard */}
      <header className="match-hero">
        <div className="ribbon">Final</div>
        <div className="side left">
          <div className="team-stripe"></div>
          <div className="team-meta">
            <div className="team-tag">Blue · Team 0</div>
            <div className="team-name">{m.team0_name}</div>
            {winnerIs0
              ? <span className="result-pill win">Win</span>
              : <span className="result-pill loss">Loss</span>}
          </div>
          <div className={"score-display tnum " + (!winnerIs0 ? "loss" : "")} style={{ marginLeft: "auto" }}>{m.team0_score}</div>
        </div>

        <div className="middle">
          <div className="final">Final · {window.MOCK.modeLabel(m.mode, m)}</div>
          <div className="vs">{window.MOCK.fmtClock(m.duration_seconds)}</div>
          <div className="meta-line">
            <span><b>{window.MOCK.arenaNice(m.arena)}</b></span>
            <span>{window.MOCK.fmtDate(m.started_at)}</span>
            <span>
              {m.is_online ? "Online" : "Offline"}
              <span className="dim"> · {m.crossbar_hits} crossbar{m.crossbar_hits !== 1 ? "s" : ""}</span>
            </span>
          </div>
        </div>

        <div className="side right">
          <div className="team-stripe"></div>
          <div className="team-meta">
            <div className="team-tag">Orange · Team 1</div>
            <div className="team-name">{m.team1_name}</div>
            {!winnerIs0
              ? <span className="result-pill win">Win</span>
              : <span className="result-pill loss">Loss</span>}
          </div>
          <div className={"score-display tnum " + (winnerIs0 ? "loss" : "")} style={{ marginRight: "auto" }}>{m.team1_score}</div>
        </div>
      </header>

      {/* Rosters */}
      <Roster team={teamBlue}  teamNum={0} name={m.team0_name} score={m.team0_score} isWinner={winnerIs0}  />
      <Roster team={teamOrng}  teamNum={1} name={m.team1_name} score={m.team1_score} isWinner={!winnerIs0} />

      {/* Per-player radars */}
      <div className="card" style={{ marginTop: 14 }}>
        <div className="section-title">
          <span>Per-player radars</span>
          <span className="dim">Scaled to this match's peaks</span>
        </div>
        <div className="eyebrow" style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ width: 8, height: 8, background: "var(--team-blue)", borderRadius: 2, display: "inline-block" }}></span>
          {m.team0_name}
        </div>
        <div className="radar-grid" style={{ marginBottom: 18 }}>
          {teamBlue.map((p, i) => <PlayerRadarCard key={i} p={p} />)}
        </div>
        <div className="eyebrow" style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ width: 8, height: 8, background: "var(--team-orng)", borderRadius: 2, display: "inline-block" }}></span>
          {m.team1_name}
        </div>
        <div className="radar-grid">
          {teamOrng.map((p, i) => <PlayerRadarCard key={i} p={p} />)}
        </div>
      </div>

      {/* Footnote — explain the SPECTATOR limitation */}
      <div className="note" style={{ marginTop: 12 }}>
        <b>Why opponents are missing movement stats:</b> the Rocket League Stats API marks boost / speed / on-wall / on-ground as
        <code style={{ background: "var(--bg)", padding: "1px 5px", borderRadius: 4, margin: "0 4px" }}>SPECTATOR</code>
        fields. Only your team's ticks are emitted, so we render the basic line for opponents and skip the advanced row.
      </div>
    </div>
  );
}

window.MatchDetail = MatchDetail;

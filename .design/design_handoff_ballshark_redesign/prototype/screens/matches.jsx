// Matches list screen — chronological list of every recorded match.

function MatchesList({ go }) {
  const SELF = window.MOCK.SELF;
  const allMatches = window.MOCK.matches;

  const [mode, setMode] = useState("all"); // all | online | offline | bots
  const [search, setSearch] = useState("");

  const filtered = useMemo(() => {
    return allMatches.filter(m => {
      if (mode === "online"   && !m.is_online) return false;
      if (mode === "offline"  && m.is_online && m.mode !== "exhibition") return false;
      if (mode === "bots"     && m.mode !== "bots") return false;
      if (search) {
        const q = search.toLowerCase();
        const hit = m.players.some(p => p.name.toLowerCase().includes(q))
          || m.team0_name.toLowerCase().includes(q)
          || m.team1_name.toLowerCase().includes(q)
          || window.MOCK.arenaNice(m.arena).toLowerCase().includes(q);
        if (!hit) return false;
      }
      return true;
    });
  }, [mode, search, allMatches]);

  // Top-of-list summary
  const myLines = filtered.map(m => ({
    m, me: m.players.find(p => p.primary_id === SELF.primary_id)
  })).filter(x => x.me);
  const wins = myLines.filter(x => x.me.team_num === x.m.winner_team_num).length;
  const totalGoals  = myLines.reduce((s, x) => s + x.me.goals, 0);
  const totalSaves  = myLines.reduce((s, x) => s + x.me.saves, 0);
  const totalAssist = myLines.reduce((s, x) => s + x.me.assists, 0);

  return (
    <div>
      <PageHead
        title="Match history"
        sub={`${allMatches.length} matches stored locally · oldest ${window.MOCK.fmtRelTime(allMatches[allMatches.length-1].started_at)}`}
      />

      <div className="toolbar">
        <div className="seg">
          {[
            ["all",     "All"],
            ["online",  "Online"],
            ["offline", "Offline"],
            ["bots",    "Bots"],
          ].map(([k, l]) => (
            <button key={k} className={mode === k ? "active" : ""} onClick={() => setMode(k)}>{l}</button>
          ))}
        </div>
        <div className="search-box">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
          <input placeholder="Search player, team name, arena…" value={search} onChange={e => setSearch(e.target.value)} />
        </div>
        <div style={{ marginLeft: "auto", fontSize: 12, color: "var(--text-dim)" }}>
          {filtered.length === allMatches.length
            ? `${allMatches.length} matches`
            : `${filtered.length} of ${allMatches.length} matches`}
        </div>
      </div>

      <div className="summary-row">
        <span>Filtered total:</span>
        <span><b className="tnum">{wins}-{filtered.length - wins}</b> · <span className="dim">{filtered.length ? ((wins/filtered.length)*100).toFixed(1) : "0.0"}% win rate</span></span>
        <span className="dim">·</span>
        <span><b className="tnum">{totalGoals}</b> goals</span>
        <span><b className="tnum">{totalAssist}</b> assists</span>
        <span><b className="tnum">{totalSaves}</b> saves</span>
      </div>

      <div className="card" style={{ padding: 0, overflow: "hidden" }}>
        <table className="history">
          <thead>
            <tr>
              <th style={{ width: 40 }}></th>
              <th style={{ width: 110 }}>When</th>
              <th>Score</th>
              <th>Arena</th>
              <th>Mode</th>
              <th>Your line</th>
              <th className="num">G</th>
              <th className="num">A</th>
              <th className="num">Sv</th>
              <th className="num">Sh</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {filtered.map(m => {
              const me  = m.players.find(p => p.primary_id === SELF.primary_id);
              if (!me) return null;
              const won = me.team_num === m.winner_team_num;
              const isBlueMe = me.team_num === 0;
              const t0Cls = m.winner_team_num === 0 ? "winner" : "";
              const t1Cls = m.winner_team_num === 1 ? "winner" : "";
              const myTeamMates = m.players.filter(p => p.team_num === me.team_num && p.name !== SELF.name).map(p => p.name).join(", ");
              return (
                <tr key={m.id} className={"row click " + (won ? "win" : "loss")} onClick={() => go(`match/${m.id}`)}>
                  <td><ResultBadge won={won} /></td>
                  <td className="dim tnum">{window.MOCK.fmtRelTime(m.started_at)}</td>
                  <td className="score-cell">
                    <span style={{ color: isBlueMe ? "var(--team-blue)" : "var(--text-dim)", fontWeight: 700 }}>{m.team0_name}</span>{" "}
                    <span className={"tnum " + t0Cls}><b>{m.team0_score}</b></span>
                    <span className="dim" style={{ margin: "0 6px" }}>–</span>
                    <span className={"tnum " + t1Cls}><b>{m.team1_score}</b></span>{" "}
                    <span style={{ color: !isBlueMe ? "var(--team-orng)" : "var(--text-dim)", fontWeight: 700 }}>{m.team1_name}</span>
                  </td>
                  <td className="dim">{window.MOCK.arenaNice(m.arena)}</td>
                  <td>
                    <Chip kind={m.mode === "1v1" ? "" : m.mode === "bots" ? "bot" : ""}>
                      {window.MOCK.modeLabel(m.mode, m)}
                    </Chip>
                  </td>
                  <td className="dim" style={{ fontSize: 12, maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {myTeamMates || "solo"}
                  </td>
                  <td className="num tnum"><b>{me.goals}</b></td>
                  <td className="num tnum"><b>{me.assists}</b></td>
                  <td className="num tnum"><b>{me.saves}</b></td>
                  <td className="num tnum"><b>{me.shots}</b></td>
                  <td>{me.is_mvp ? <Chip kind="mvp">MVP</Chip> : ""}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

window.MatchesList = MatchesList;

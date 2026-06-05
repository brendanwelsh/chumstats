// Overlays screen — showcases the 4 OBS browser-source modes on a faux RL field.

function OverlaysShowcase({ go }) {
  const SELF = window.MOCK.SELF;
  // Use the most recent match as the "live" / "last" data source.
  const m = window.MOCK.matches[0];
  const meIdx = m.players.findIndex(p => p.primary_id === SELF.primary_id);
  const me = m.players[meIdx];

  // Compute a session from the last ~6 matches.
  const sessionMatches = window.MOCK.matches.slice(0, 6).reverse();
  const sessionLines   = sessionMatches.map(mt => ({
    mt, me: mt.players.find(p => p.primary_id === SELF.primary_id)
  })).filter(x => x.me);
  const wins   = sessionLines.filter(x => x.me.team_num === x.mt.winner_team_num).length;
  const losses = sessionLines.length - wins;
  const session = {
    wins, losses,
    streak: 3, // most recent winning streak (mocked)
    goals:   sessionLines.reduce((s, x) => s + x.me.goals, 0),
    assists: sessionLines.reduce((s, x) => s + x.me.assists, 0),
    saves:   sessionLines.reduce((s, x) => s + x.me.saves, 0),
    shots:   sessionLines.reduce((s, x) => s + x.me.shots, 0),
    demos:   sessionLines.reduce((s, x) => s + x.me.demos, 0),
    form: sessionLines.map(x => x.me.team_num === x.mt.winner_team_num),
  };

  const [active, setActive] = useState("live");
  // "live" shows scoreboard at mid-match; "last" final scoreboard; "session"; "me"

  // For "live" we synthesize a frozen mid-match state.
  const liveData = {
    ...m,
    team0_score: m.team0_score > 0 ? m.team0_score - 1 : 0,
    team1_score: m.team1_score > 0 ? Math.max(m.team1_score - 2, 0) : 0,
    clock: 198, // 3:18 remaining
  };
  const lastData = m;

  // Renderers for each scoreboard variant ---------------------

  function Scoreboard({ data, isFinal, clock }) {
    const players = data.players;
    const t0 = players.filter(p => p.team_num === 0).sort((a, b) => b.score - a.score);
    const t1 = players.filter(p => p.team_num === 1).sort((a, b) => b.score - a.score);
    const won = me.team_num === data.winner_team_num;
    const mm = clock != null ? Math.floor(clock / 60) : Math.floor(m.duration_seconds / 60);
    const ss = clock != null ? Math.floor(clock % 60) : Math.floor(m.duration_seconds % 60);

    return (
      <>
        <div className="bh">
          <div className="bh-team t0">{data.team0_name}</div>
          <div className="bh-center">
            <span className="sc t0 tnum">{data.team0_score}</span>
            <span className="clock">{mm}:{ss.toString().padStart(2, "0")}</span>
            <span className="sc t1 tnum">{data.team1_score}</span>
          </div>
          <div className="bh-team t1">{data.team1_name}</div>
        </div>
        <div className="bb">
          {[t0, t1].map((team, idx) => (
            <table key={idx} className={"t" + idx}>
              <thead>
                <tr>
                  <th className="left">Player</th>
                  <th>Sc</th>
                  <th>G</th>
                  <th>Sh</th>
                  <th>A</th>
                  <th>Sv</th>
                </tr>
              </thead>
              <tbody>
                {team.slice(0, 3).map((p, i) => (
                  <tr key={i} className={p.primary_id === SELF.primary_id ? "you" : ""}>
                    <td className="left">{p.name.replace(/^@/, "")}{p.is_mvp ? <span className="mvp-star">★</span> : ""}</td>
                    <td>{p.score}</td><td>{p.goals}</td><td>{p.shots}</td><td>{p.assists}</td><td>{p.saves}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ))}
        </div>
        {isFinal && (
          <div className="bf">
            <span className={"result " + (won ? "win" : "loss")}>{won ? "Win" : "Loss"}</span>
            <span>{window.MOCK.arenaNice(data.arena)} · {data.is_online ? "Online" : "Offline"} · {Math.floor(data.duration_seconds/60)}:{(data.duration_seconds%60).toFixed(0).padStart(2,"0")}</span>
          </div>
        )}
      </>
    );
  }

  function SessionCard() {
    return (
      <>
        <div className="sess-row">
          <span className="sess-wl tnum">{session.wins}-{session.losses}</span>
          <span className="sess-div"></span>
          <div className="sess-streak"><b>{session.streak}W</b><span>streak</span></div>
          <span className="sess-div"></span>
          <div className="sess-form">
            {session.form.map((w, i) => <span key={i} className={"d " + (w ? "w" : "l")} />)}
          </div>
        </div>
        <div className="sess-totals">
          Goals <b style={{ color: "#fff" }}>{session.goals}</b> ·{" "}
          Assists <b style={{ color: "#fff" }}>{session.assists}</b> ·{" "}
          Saves <b style={{ color: "#fff" }}>{session.saves}</b> ·{" "}
          Shots <b style={{ color: "#fff" }}>{session.shots}</b> ·{" "}
          Demos <b style={{ color: "#fff" }}>{session.demos}</b>
        </div>
      </>
    );
  }

  function MeCard() {
    return (
      <>
        <span className="me-name">{me.name.replace(/^@/, "")}</span>
        <div className="me-line">
          <span><b className="tnum">{me.goals}</b><small>G</small></span>
          <span><b className="tnum">{me.assists}</b><small>A</small></span>
          <span><b className="tnum">{me.saves}</b><small>Sv</small></span>
          <span><b className="tnum">{me.shots}</b><small>Sh</small></span>
        </div>
      </>
    );
  }

  return (
    <div>
      <PageHead
        title="Browser overlay"
        sub="Drop into OBS as a Browser Source. Transparent background, 4 layouts. Click a tab to preview each on a darkened arena."
      />

      <div className="overlay-stage" data-screen-label="Overlay stage">
        <div className="crowd"></div>
        <div className="field"></div>
        <div className="stagelabel">
          <span className="obs">OBS · 1920×1080 Browser Source</span>
          <span>Ballshark overlay — transparent bg, ~30 fps WebSocket push</span>
        </div>

        {active === "live" && (
          <div className="ov-card live" style={{ position: "absolute", top: 60, left: "50%", transform: "translateX(-50%)" }}>
            <div className="ov-tag">/overlay/live</div>
            <Scoreboard data={liveData} clock={liveData.clock} isFinal={false} />
          </div>
        )}

        {active === "last" && (
          <div className="ov-card last" style={{ position: "absolute", top: 60, left: "50%", transform: "translateX(-50%)" }}>
            <div className="ov-tag">/overlay/last</div>
            <Scoreboard data={lastData} isFinal={true} />
          </div>
        )}

        {active === "session" && (
          <div className="ov-card sess" style={{ position: "absolute", top: 60, left: "50%", transform: "translateX(-50%)", width: 360 }}>
            <div className="ov-tag">/overlay/session</div>
            <SessionCard />
          </div>
        )}

        {active === "me" && (
          <div className="ov-card me" style={{ position: "absolute", top: 60, left: "50%", transform: "translateX(-50%)", width: 280 }}>
            <div className="ov-tag">/overlay/me</div>
            <MeCard />
          </div>
        )}

        <div className="obs-toolbar">
          {[
            { k: "live",    l: "Live" },
            { k: "last",    l: "Last match" },
            { k: "session", l: "Session" },
            { k: "me",      l: "Me only" },
          ].map(t => (
            <div key={t.k}
                 className={"obs-tab" + (active === t.k ? " active" : "")}
                 onClick={() => setActive(t.k)}>
              {t.l}
            </div>
          ))}
        </div>
      </div>

      <div className="card" style={{ marginTop: 18 }}>
        <div className="section-title">
          <span>OBS source URLs</span>
          <span className="dim">Pick the one that matches the layout you want</span>
        </div>
        <div className="overlay-paths">
          {[
            { name: "Live scoreboard", path: "http://127.0.0.1:5050/overlay/live",
              note: "Full BARL-style box with both rosters + score + clock. Updates at ~4 Hz during play." },
            { name: "Last-match scoreboard", path: "http://127.0.0.1:5050/overlay/last",
              note: "Identical layout, frozen on the final state. Use between matches for highlight cards." },
            { name: "Session tracker", path: "http://127.0.0.1:5050/overlay/session",
              note: "Compact W-L + streak + form dots. No in-match data. Great for the corner of a stream." },
            { name: "Me only", path: "http://127.0.0.1:5050/overlay/me",
              note: "Tiny pill with your stat line. Cleanest option if you don't want opponent info visible." },
          ].map(p => (
            <div key={p.name} className="pcard">
              <div className="head"><span>{p.name}</span></div>
              <code>{p.path}</code>
              <p>{p.note}</p>
            </div>
          ))}
        </div>
      </div>

      <div className="note" style={{ marginTop: 12 }}>
        <b>How it works:</b> the overlay subscribes to <code>ws://127.0.0.1:5050/ws</code>. The ingest pipeline broadcasts
        <code>tick</code> / <code>match_end</code> / <code>session</code> / <code>goal</code> events. New OBS sources get
        the last known state on connect (sticky state on the server), so reloading mid-match doesn't blank out.
      </div>
    </div>
  );
}

window.OverlaysShowcase = OverlaysShowcase;

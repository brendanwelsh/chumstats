// Carball Tracker overlay client. Connects to /ws and renders one of four
// modes based on the URL path:
//   /overlay/live    full BARL-style scoreboard during a match (live ticks)
//   /overlay/last    BARL scoreboard frozen on last match's final state
//   /overlay/session session totals + last-10 form (no in-match data)
//   /overlay/me      tiny pill with just your stat line
(() => {
  const SELF_NAME = (window.CARBALL_SELF_NAME || "").toLowerCase();
  const path = window.location.pathname;
  const mode = (path.match(/\/overlay\/(\w+)/) || [, "last"])[1];
  document.body.classList.add("mode-" + mode);

  const els = {
    bhT0Name: document.getElementById("bh-t0-name"),
    bhT1Name: document.getElementById("bh-t1-name"),
    bhT0Score: document.getElementById("bh-t0-score"),
    bhT1Score: document.getElementById("bh-t1-score"),
    bhClock: document.getElementById("bh-clock"),
    bhOt: document.getElementById("bh-ot"),
    btRows0: document.getElementById("bt-0-rows"),
    btRows1: document.getElementById("bt-1-rows"),
    bf: document.getElementById("bf"),
    bfResult: document.getElementById("bf-result"),
    bfMeta: document.getElementById("bf-meta"),

    sessWl: document.getElementById("sess-wl"),
    sessStreak: document.getElementById("sess-streak"),
    sessForm: document.getElementById("sess-form"),
    sessTotals: document.getElementById("sess-totals"),

    meName: document.getElementById("me-name"),
    meG: document.getElementById("me-g"),
    meA: document.getElementById("me-a"),
    meSv: document.getElementById("me-sv"),
    meSh: document.getElementById("me-sh"),
    meD: document.getElementById("me-d"),

    toast: document.getElementById("toast"),
  };

  function findMe(players) {
    if (!players || !players.length) return null;
    if (SELF_NAME) {
      for (const p of players) if ((p.name || "").toLowerCase() === SELF_NAME) return p;
    }
    for (const p of players) if (!p.is_bot && (p.primary_id || "").indexOf("Unknown") !== 0) return p;
    return players[0] || null;
  }

  function isMeRow(p, me) {
    if (!me) return false;
    return p.name === me.name && p.team_num === me.team_num;
  }

  function teamPlayers(players, teamNum) {
    return players
      .filter((p) => p.team_num === teamNum)
      .sort((a, b) => (b.score || 0) - (a.score || 0));
  }

  function renderTeamRow(p, me, mvpMap) {
    const isMe = isMeRow(p, me);
    const mvp = mvpMap && mvpMap[p.primary_id] && !p.is_bot;
    const tr = document.createElement("tr");
    if (isMe) tr.classList.add("is-me");
    const safeName = (p.name || "").replace(/</g, "&lt;");
    const bot = p.is_bot ? '<span class="bot-tag">BOT</span>' : "";
    const mvpStar = mvp ? '<span class="mvp-star">★</span>' : "";
    tr.innerHTML = `
      <td class="bt-name">${safeName}${bot}${mvpStar}</td>
      <td>${p.score ?? 0}</td>
      <td>${p.goals ?? 0}</td>
      <td>${p.shots ?? 0}</td>
      <td>${p.assists ?? 0}</td>
      <td>${p.saves ?? 0}</td>
    `;
    return tr;
  }

  function renderScoreboard(data, isFinal) {
    if (!data) return;
    const t0Name = data.team0_name || "Blue";
    const t1Name = data.team1_name || "Orange";
    const t0Score = data.team0_score ?? 0;
    const t1Score = data.team1_score ?? 0;
    els.bhT0Name.textContent = t0Name;
    els.bhT1Name.textContent = t1Name;
    els.bhT0Score.textContent = t0Score;
    els.bhT1Score.textContent = t1Score;

    if (typeof data.time_seconds === "number") {
      const t = Math.max(0, data.time_seconds);
      const mm = Math.floor(t / 60);
      const ss = Math.floor(t % 60);
      els.bhClock.textContent = `${mm}:${ss.toString().padStart(2, "0")}`;
    } else {
      els.bhClock.textContent = "--:--";
    }
    if (data.is_overtime) els.bhOt.hidden = false; else els.bhOt.hidden = true;

    const players = data.players || [];
    const me = findMe(players);
    const mvpMap = data.is_mvp || {};

    els.btRows0.innerHTML = "";
    els.btRows1.innerHTML = "";
    for (const p of teamPlayers(players, 0)) els.btRows0.appendChild(renderTeamRow(p, me, mvpMap));
    for (const p of teamPlayers(players, 1)) els.btRows1.appendChild(renderTeamRow(p, me, mvpMap));

    if (isFinal && me) {
      const won = me.team_num === data.winner_team_num;
      els.bfResult.textContent = won ? "WIN" : "LOSS";
      els.bfResult.className = "bf-result " + (won ? "win" : "loss");
      const arena = (data.arena || "").toString();
      const mode = data.is_online ? "Online" : "Offline";
      const dur = (data.duration_seconds || 0);
      const mm = Math.floor(dur / 60), ss = Math.floor(dur % 60);
      els.bfMeta.textContent = `${arena}  ·  ${mode}  ·  ${mm}:${ss.toString().padStart(2, "0")}`;
    }
  }

  function renderSession(t) {
    if (!t) return;
    els.sessWl.textContent = `${t.wins || 0}-${t.losses || 0}`;
    els.sessStreak.firstChild.textContent = t.streak_label || streakLabel(t.current_streak);
    els.sessTotals.textContent =
      `Goals ${t.goals || 0}  ·  Assists ${t.assists || 0}  ·  Saves ${t.saves || 0}  `
      + `·  Shots ${t.shots || 0}  ·  Demos ${t.demos || 0}`;
  }

  function renderForm(dots) {
    // dots is a string like "🟢🟢🔴🟢"; convert to spans with explicit colors
    // for non-emoji rendering. Pass through if it's already markers.
    els.sessForm.innerHTML = "";
    if (!dots) return;
    for (const ch of dots) {
      const span = document.createElement("span");
      if (ch === "🟢" || ch === "W" || ch === "✓") { span.className = "w"; span.textContent = "✓"; }
      else if (ch === "🔴" || ch === "L" || ch === "✗") { span.className = "l"; span.textContent = "✗"; }
      else continue;
      els.sessForm.appendChild(span);
    }
  }

  function renderMe(data) {
    if (!data) return;
    const players = data.players || [];
    const me = findMe(players);
    if (!me) return;
    // Strip RL's leading "@" since it looks weird as an overlay header.
    const cleanName = (me.name || "You").replace(/^@/, "");
    els.meName.textContent = cleanName;
    els.meG.textContent = me.goals ?? 0;
    els.meA.textContent = me.assists ?? 0;
    els.meSv.textContent = me.saves ?? 0;
    els.meSh.textContent = me.shots ?? 0;
  }

  function streakLabel(n) {
    if (!n) return "—";
    return `${Math.abs(n)}${n > 0 ? "W" : "L"}`;
  }

  function flashToast(text, klass) {
    if (!els.toast) return;
    els.toast.className = `toast ${klass || ""}`;
    els.toast.textContent = text;
    els.toast.hidden = false;
    requestAnimationFrame(() => els.toast.classList.add("show"));
    clearTimeout(flashToast._t);
    flashToast._t = setTimeout(() => {
      els.toast.classList.remove("show");
      setTimeout(() => { els.toast.hidden = true; }, 260);
    }, 2200);
  }

  function connect() {
    const ws = new WebSocket(`ws://${location.host}/ws`);
    ws.addEventListener("open",  () => { document.body.classList.remove("conn-error"); document.body.classList.add("conn-open"); });
    ws.addEventListener("close", () => { document.body.classList.remove("conn-open"); setTimeout(connect, 1500); });
    ws.addEventListener("error", () => { document.body.classList.add("conn-error"); });
    ws.addEventListener("message", (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch { return; }
      if (!msg || !msg.type) return;
      switch (msg.type) {
        case "tick":
          if (mode === "live") renderScoreboard(msg.data, false);
          if (mode === "me")   renderMe(msg.data);
          break;
        case "match_end":
          if (mode === "last") renderScoreboard(msg.data, true);
          break;
        case "session":
          if (mode === "session") renderSession(msg.data);
          break;
        case "goal":
          if (mode === "live" || mode === "last") {
            const d = msg.data || {};
            const who = (d.Scorer && d.Scorer.Name) || (d.scorer && d.scorer.name) || "Goal";
            const speed = d.GoalSpeed || d.goal_speed || 0;
            flashToast(`${who}  ·  ${Math.round(speed)} kph`, "goal");
            document.body.classList.add("flash-goal");
            setTimeout(() => document.body.classList.remove("flash-goal"), 900);
          }
          break;
        case "crossbar":
          if (mode === "live" || mode === "last") flashToast("CROSSBAR", "crossbar");
          break;
      }
    });
  }

  // For session mode we also fetch form dots once from the dashboard API.
  if (mode === "session") {
    fetch("/api/dashboard").then((r) => r.json()).then((j) => {
      const groups = j.groups || {};
      const recent = (groups["Recent form (last 10)"] || []).find((x) => (x.label || "").startsWith("Last "));
      if (recent) renderForm(recent.value);
    }).catch(() => {});
  }

  connect();
})();

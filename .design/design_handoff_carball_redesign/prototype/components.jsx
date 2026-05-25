// Shared components: layout, navigation, charts, chips.
// Globalized to `window` so other Babel scripts can use them.

const { useState, useEffect, useMemo, useRef, useCallback } = React;

// ============================================================
// Navigation
// ============================================================
function Nav({ route, go }) {
  const items = [
    { key: "dashboard", label: "Me" },
    { key: "matches",   label: "Matches" },
    { key: "players",   label: "Players" },
    { key: "overlays",  label: "Overlay" },
  ];
  const active = route.startsWith("match/") ? "matches"
    : route.startsWith("player/") ? "players"
    : route;

  return (
    <nav className="topnav">
      <a className="brand" onClick={() => go("dashboard")}>
        <div className="brand-logo">C</div>
        <div>
          <div className="brand-name">Carball Tracker</div>
          <div className="brand-sub">v0.3 · self-hosted</div>
        </div>
      </a>
      <div className="navlinks">
        {items.map(it => (
          <a key={it.key}
             className={"navlink" + (active === it.key ? " active" : "")}
             onClick={() => go(it.key)}>
            {it.label}
          </a>
        ))}
      </div>
      <div className="nav-aside">
        <span className="live-pip off" title="No active match">
          <span className="dot"></span>
          idle
        </span>
        <ThemeToggle />
      </div>
    </nav>
  );
}

// ============================================================
// Theme toggle (writes data-theme on documentElement + localStorage)
// ============================================================
function ThemeToggle() {
  const [theme, setTheme] = useState(() =>
    document.documentElement.getAttribute("data-theme") || "dark"
  );

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("carball-theme", theme); } catch (e) {}
  }, [theme]);

  return (
    <button className="theme-toggle"
            onClick={() => setTheme(t => t === "dark" ? "light" : "dark")}
            aria-label="Toggle theme">
      {theme === "dark"
        ? <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>
        : <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
      }
      <span>{theme === "dark" ? "Light" : "Dark"}</span>
    </button>
  );
}

// ============================================================
// Logo (re-used in overlay scoreboards)
// ============================================================
function Logo({ size = 24 }) {
  return (
    <div className="brand-logo" style={{ width: size, height: size, fontSize: size * 0.5, borderRadius: size * 0.22 }}>C</div>
  );
}

// ============================================================
// Radar chart — SVG, per-axis scaling so each metric has its own peak.
// `values`: [{ label, value, max, suffix? }]
// ============================================================
function Radar({ values, size = 280, color, strokeWidth = 2, fillOpacity = 0.18, showLabels = true, axisFont = 11 }) {
  const cx = size / 2, cy = size / 2;
  const r  = size * 0.34;
  const n  = values.length;
  if (n < 3) return null;
  const accent = color || "var(--accent)";

  const points = values.map((v, i) => {
    const a = -Math.PI / 2 + (2 * Math.PI * i) / n;
    const scale = Math.max(0, Math.min(1, v.max ? v.value / v.max : 0));
    return {
      a,
      x: cx + r * scale * Math.cos(a),
      y: cy + r * scale * Math.sin(a),
      lx: cx + (r + 22) * Math.cos(a),
      ly: cy + (r + 22) * Math.sin(a),
      sx: cx + r * Math.cos(a),
      sy: cy + r * Math.sin(a),
      anchor: Math.cos(a) > 0.3 ? "start" : Math.cos(a) < -0.3 ? "end" : "middle",
      ...v,
    };
  });

  const ringPath = (pct) => points
    .map((p, i) => {
      const x = cx + r * pct * Math.cos(p.a);
      const y = cy + r * pct * Math.sin(p.a);
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ") + " Z";

  const polyPoints = points.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");

  return (
    <svg viewBox={`0 0 ${size} ${size}`} style={{ width: "100%", maxWidth: size, display: "block", margin: "0 auto" }}>
      {/* rings */}
      {[0.25, 0.5, 0.75, 1.0].map(p => (
        <path key={p} d={ringPath(p)} fill="none" stroke="var(--border)" strokeWidth="1" />
      ))}
      {/* spokes */}
      {points.map((p, i) => (
        <line key={i} x1={cx} y1={cy} x2={p.sx} y2={p.sy} stroke="var(--border)" strokeWidth="1" />
      ))}
      {/* data polygon */}
      <polygon points={polyPoints} fill={accent} fillOpacity={fillOpacity} stroke={accent} strokeWidth={strokeWidth} strokeLinejoin="round" />
      {points.map((p, i) => (
        <circle key={i} cx={p.x} cy={p.y} r="3" fill={accent} />
      ))}
      {/* labels */}
      {showLabels && points.map((p, i) => (
        <g key={i}>
          <text x={p.lx} y={p.ly - 4} textAnchor={p.anchor}
                fill="var(--text)" fontSize={axisFont} fontWeight="700"
                style={{ letterSpacing: "0.04em", textTransform: "uppercase" }}>
            {p.label}
          </text>
          <text x={p.lx} y={p.ly + 10} textAnchor={p.anchor}
                fill="var(--text-dim)" fontSize={axisFont - 1} fontWeight="500"
                style={{ fontVariantNumeric: "tabular-nums" }}>
            {p.value < 10 ? p.value.toFixed(2) : p.value.toFixed(0)}
            {p.suffix || ""}
          </text>
        </g>
      ))}
    </svg>
  );
}

// ============================================================
// Form dots (recent W/L sequence)
// ============================================================
function FormDots({ results, max = 10 }) {
  // results: array of booleans (true=win), most recent first.
  const padded = results.slice(0, max);
  while (padded.length < max) padded.push(null);
  return (
    <div className="form-dots" title={`Last ${results.length} matches`}>
      {padded.slice().reverse().map((r, i) => (
        <span key={i} className={"d " + (r === null ? "tbd" : r ? "win" : "loss")} />
      ))}
    </div>
  );
}

// ============================================================
// Sparkbar — vertical bars for goals/assists/scores over time
// ============================================================
function Sparkbar({ values, highlight }) {
  const max = Math.max(1, ...values);
  return (
    <div className="sparkbar">
      {values.map((v, i) => (
        <span key={i}
              className={"b" + (highlight && highlight[i] ? " hi" : "")}
              style={{ height: `${(v / max) * 100}%` }} />
      ))}
    </div>
  );
}

// ============================================================
// Stacked bar — for movement / boost breakdowns
// segments: [{label, pct, color}]
// ============================================================
function Stack({ segments }) {
  return (
    <div>
      <div className="stack">
        {segments.map((s, i) => (
          <span key={i} style={{ width: `${s.pct * 100}%`, background: s.color }} />
        ))}
      </div>
      <div className="stack-legend">
        {segments.map((s, i) => (
          <span key={i}>
            <i className="dot" style={{ background: s.color }} />
            {s.label} <b style={{ color: "var(--text)", marginLeft: 2 }}>{(s.pct * 100).toFixed(0)}%</b>
          </span>
        ))}
      </div>
    </div>
  );
}

// ============================================================
// Chip
// ============================================================
function Chip({ kind, children }) {
  return <span className={"chip" + (kind ? " " + kind : "")}>{children}</span>;
}

// ============================================================
// Result badge (W/L block)
// ============================================================
function ResultBadge({ won }) {
  return <span className={"badge " + (won ? "win" : "loss")}>{won ? "W" : "L"}</span>;
}

// ============================================================
// PlayerLink — clickable name; if it's the owner, accent it.
// ============================================================
function PlayerLink({ name, primary_id, is_bot, go }) {
  const isSelf = primary_id === window.MOCK.SELF.primary_id;
  if (is_bot) {
    return <span className="player-link" style={{ cursor: "default", color: "var(--text-faint)" }}>{name}</span>;
  }
  return (
    <a className={"player-link" + (isSelf ? " self" : "")}
       onClick={(e) => { e.stopPropagation(); go(`player/${encodeURIComponent(name)}`); }}>
      {name}
    </a>
  );
}

// ============================================================
// Compact arena pip
// ============================================================
function ArenaLabel({ arena }) {
  return <span className="dim" style={{ fontSize: 12 }}>{window.MOCK.arenaNice(arena)}</span>;
}

// ============================================================
// Page header
// ============================================================
function PageHead({ title, sub, right }) {
  return (
    <div className="page-head">
      <div>
        <h1>{title}</h1>
        {sub && <div className="sub">{sub}</div>}
      </div>
      {right && <div className="right">{right}</div>}
    </div>
  );
}

// ============================================================
// KPI tile
// ============================================================
function KPI({ label, value, foot, primary }) {
  return (
    <div className={"kpi" + (primary ? " primary" : "")}>
      <div className="kpi-label">{label}</div>
      <div className="kpi-value">{value}</div>
      {foot && <div className="kpi-foot">{foot}</div>}
    </div>
  );
}

// ============================================================
// Stat row used inside small statgrid panels
// ============================================================
function StatRow({ k, v, c }) {
  return (
    <React.Fragment>
      <div className="k">{k}</div>
      <div className="v tnum">{v}</div>
      <div className="c">{c}</div>
    </React.Fragment>
  );
}

Object.assign(window, {
  Nav, ThemeToggle, Logo, Radar, FormDots, Sparkbar, Stack,
  Chip, ResultBadge, PlayerLink, ArenaLabel, PageHead, KPI, StatRow,
});

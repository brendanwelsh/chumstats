// Carball Tracker — main app shell. Hash routing, theme bootstrap, tweaks.

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "density":      "comfortable",
  "accent":       "restrained",
  "cardStyle":    "bordered",
  "teamWashes":   false
}/*EDITMODE-END*/;

// Apply tweaks as body class toggles so CSS can react.
function applyTweaks(t) {
  document.body.classList.toggle("compact", t.density === "compact");
  document.body.classList.toggle("bold",     t.accent  === "bold");
  document.body.classList.toggle("flat",     t.cardStyle === "flat");
  document.body.classList.toggle("washes",   !!t.teamWashes);
}

// Bootstrap theme before React mounts to avoid the flash.
(function bootstrapTheme() {
  let saved = null;
  try { saved = localStorage.getItem("carball-theme"); } catch (e) {}
  if (!saved) saved = "dark";
  document.documentElement.setAttribute("data-theme", saved);
})();

function App() {
  // ----- routing (hash-based) -----
  const [route, setRoute] = useState(() =>
    (location.hash || "#dashboard").slice(1) || "dashboard"
  );
  useEffect(() => {
    const onHash = () => setRoute((location.hash || "#dashboard").slice(1) || "dashboard");
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const go = (r) => { location.hash = "#" + r; window.scrollTo(0, 0); };

  // ----- tweaks -----
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
  useEffect(() => { applyTweaks(t); }, [t]);

  // ----- screen dispatch -----
  let screen;
  if (route === "dashboard") {
    screen = <Dashboard go={go} tweaks={t} />;
  } else if (route === "matches") {
    screen = <MatchesList go={go} />;
  } else if (route.startsWith("match/")) {
    screen = <MatchDetail matchId={route.slice(6)} go={go} />;
  } else if (route === "players") {
    screen = <PlayersDirectory go={go} />;
  } else if (route.startsWith("player/")) {
    screen = <PlayerProfile name={decodeURIComponent(route.slice(7))} go={go} />;
  } else if (route === "overlays") {
    screen = <OverlaysShowcase go={go} />;
  } else {
    screen = <Dashboard go={go} tweaks={t} />;
  }

  return (
    <div className="app-shell" data-screen-label={routeToLabel(route)}>
      <Nav route={route} go={go} />
      {screen}

      <TweaksPanel>
        <TweakSection label="Density">
          <TweakRadio label="Spacing" value={t.density}
            options={[{ value: "comfortable", label: "Comfy" },
                      { value: "compact",     label: "Compact" }]}
            onChange={(v) => setTweak("density", v)} />
        </TweakSection>
        <TweakSection label="Accent intensity">
          <TweakRadio label="Look" value={t.accent}
            options={[{ value: "restrained", label: "Restrained" },
                      { value: "bold",       label: "Bold" }]}
            onChange={(v) => setTweak("accent", v)} />
        </TweakSection>
        <TweakSection label="Card style">
          <TweakRadio label="Surfaces" value={t.cardStyle}
            options={[{ value: "bordered", label: "Bordered" },
                      { value: "flat",     label: "Flat" }]}
            onChange={(v) => setTweak("cardStyle", v)} />
        </TweakSection>
        <TweakSection label="Match-detail hero">
          <TweakToggle label="Team-color washes" value={t.teamWashes}
            onChange={(v) => setTweak("teamWashes", v)} />
        </TweakSection>
      </TweaksPanel>
    </div>
  );
}

function routeToLabel(route) {
  if (route === "dashboard") return "Me / Dashboard";
  if (route === "matches")   return "Matches list";
  if (route.startsWith("match/")) return "Match detail";
  if (route === "players")   return "Players directory";
  if (route.startsWith("player/")) return "Player profile";
  if (route === "overlays")  return "Browser overlay";
  return route;
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);

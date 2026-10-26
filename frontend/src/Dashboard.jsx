import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import axios from "axios";
import { Toaster, toast } from "sonner";
import {
  RefreshCw, Settings, Send, Clock, Radio, TrendingDown,
  AlertCircle, CheckCircle2, XCircle, Activity, Filter, Power, Volume2, VolumeX,
  ChevronDown, Layers,
} from "lucide-react";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger, DialogFooter,
} from "@/components/ui/dialog";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const BACKEND_URL = import.meta.env.VITE_BACKEND_URL || "http://localhost:8000";
const API = `${BACKEND_URL}/api`;

const PROVIDER_LABELS = { theoddsapi: "The Odds API", oddspapi: "OddsPapi" };
// Alert-log source pill per backend provider.
const SOURCE_PILL = {
  prediction: { label: "Polymarket", color: "#FF2D55", title: "Fonte quote: Polymarket (prediction market)" },
  propline: { label: "Book · props", color: "#BF5AF2", title: "Fonte quote: bookmaker USA via PropLine (regola multi-book)" },
  default: { label: "Pinnacle", color: "#007AFF", title: "Fonte quote: Pinnacle (sharp book)" },
};
const SPORT_EMOJI = { tennis: "🎾", basketball: "🏀", football: "⚽", hockey: "🏒", volley: "🏐", f1: "🏎️", mlb: "⚾", nba_props: "🏀", props_nba: "🏀", props_seriea: "⚽", props_epl: "⚽", outright: "🏆" };

// A match is "underway/over" for the alert log if its start time has passed,
// or if it's an old pre-fix alert (no start_epoch) older than a few hours -
// every match from that long ago has certainly finished.
function isUnderway(a, nowMs) {
  if (a.start_epoch) return a.start_epoch * 1000 <= nowMs;
  return nowMs - new Date(a.created_at).getTime() > 6 * 3600 * 1000;
}

function fmtTime(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
function minsUntil(ts) {
  if (!ts) return null;
  return Math.round((ts * 1000 - Date.now()) / 60000);
}
function fmtCountdown(seconds) {
  if (seconds == null || seconds < 0) return "—";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

export default function Dashboard() {
  const [status, setStatus] = useState(null);
  const [settings, setSettings] = useState(null);
  const [snapshot, setSnapshot] = useState(null);
  const [alerts, setAlerts] = useState([]);
  const [refreshing, setRefreshing] = useState(false);
  const [minDrop, setMinDrop] = useState(0);
  const [onlyDrops, setOnlyDrops] = useState(false);
  const [hideStarted, setHideStarted] = useState(false);
  const [now, setNow] = useState(Date.now());
  const scanningRef = useRef(false);
  const [isDesktop] = useState(() => {
    try { return window.matchMedia("(min-width: 1024px)").matches; } catch { return true; }
  });
  const [soundEnabled, setSoundEnabled] = useState(() => {
    try { return localStorage.getItem("soundEnabled") !== "false"; } catch { return true; }
  });
  const [customSound, setCustomSound] = useState(() => {
    try { return localStorage.getItem("alertSound") || null; } catch { return null; }
  });
  const audioCtxRef = useRef(null);
  const lastAlertRef = useRef(null);
  const quotaWarnRef = useRef(null);

  // Full reload (incl. settings): on open, after every change, and when the
  // tab comes back to the foreground.
  const loadAll = useCallback(async () => {
    try {
      const [s, cfg, snap, alertsResp] = await Promise.all([
        axios.get(`${API}/status`),
        axios.get(`${API}/settings`),
        axios.get(`${API}/snapshot`),
        axios.get(`${API}/alerts?limit=100`),
      ]);
      setStatus(s.data);
      setSettings(cfg.data);
      setSnapshot(snap.data);
      setAlerts(alertsResp.data.alerts || []);
    } catch (e) {
      console.error(e);
    }
  }, []);

  // Periodic poll. Settings are left out (they only change through this UI,
  // which reloads them - and re-fetching them every 15s kept resetting the
  // Settings dialog fields while you were typing). In a background tab only
  // the alerts are polled: enough to keep the new-alert sound working without
  // pulling the whole snapshot for a page nobody is looking at.
  const poll = useCallback(async () => {
    try {
      if (document.hidden) {
        const alertsResp = await axios.get(`${API}/alerts?limit=100`);
        setAlerts(alertsResp.data.alerts || []);
        return;
      }
      const [s, snap, alertsResp] = await Promise.all([
        axios.get(`${API}/status`),
        axios.get(`${API}/snapshot`),
        axios.get(`${API}/alerts?limit=100`),
      ]);
      setStatus(s.data);
      setSnapshot(snap.data);
      setAlerts(alertsResp.data.alerts || []);
    } catch (e) {
      console.error(e);
    }
  }, []);

  useEffect(() => { loadAll(); }, [loadAll]);
  useEffect(() => {
    const t = setInterval(poll, 15000);
    const onVisible = () => { if (!document.hidden) loadAll(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(t);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [poll, loadAll]);
  // Coarse clock for hiding started matches/alerts: minute-level precision is
  // plenty, and a 1s tick re-rendered the whole dashboard every second. The
  // per-second countdown lives in its own tiny <Countdown> component.
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30000);
    return () => clearInterval(t);
  }, []);

  // Browsers block audio until a user gesture: unlock on the first interaction.
  useEffect(() => {
    const unlock = () => {
      try {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!audioCtxRef.current && AC) audioCtxRef.current = new AC();
        if (audioCtxRef.current && audioCtxRef.current.state === "suspended") {
          audioCtxRef.current.resume();
        }
      } catch { /* ignore */ }
    };
    window.addEventListener("pointerdown", unlock, { once: true });
    return () => window.removeEventListener("pointerdown", unlock);
  }, []);

  // Synthesized two-tone chime (no audio file needed).
  const playChime = useCallback(() => {
    try {
      let ctx = audioCtxRef.current;
      if (!ctx) {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return;
        ctx = new AC();
        audioCtxRef.current = ctx;
      }
      if (ctx.state === "suspended") ctx.resume();
      const t0 = ctx.currentTime;
      [880, 1320].forEach((freq, i) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.value = freq;
        const t = t0 + i * 0.16;
        gain.gain.setValueAtTime(0.0001, t);
        gain.gain.exponentialRampToValueAtTime(0.3, t + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.24);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(t);
        osc.stop(t + 0.26);
      });
    } catch (e) {
      /* audio not available / blocked */
    }
  }, []);

  // Play the uploaded custom sound if set, otherwise the synthesized chime.
  const playAlertSound = useCallback(() => {
    if (customSound) {
      try {
        const a = new Audio(customSound);
        a.play().catch(() => {});
        return;
      } catch { /* fall through to chime */ }
    }
    playChime();
  }, [customSound, playChime]);

  const toggleSound = () => {
    const next = !soundEnabled;
    setSoundEnabled(next);
    try { localStorage.setItem("soundEnabled", next ? "true" : "false"); } catch { /* ignore */ }
    if (next) playAlertSound();  // user gesture: unlocks audio + confirms
  };

  const uploadSound = (file) => {
    if (!file) return;
    if (!file.type.startsWith("audio/")) { toast.error("Serve un file audio"); return; }
    if (file.size > 1024 * 1024) { toast.error("File troppo grande (max ~1 MB). Usa un clip breve."); return; }
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = reader.result;
      try {
        localStorage.setItem("alertSound", dataUrl);
      } catch {
        toast.error("Impossibile salvare: file troppo grande per il browser.");
        return;
      }
      setCustomSound(dataUrl);
      toast.success("Suono alert personalizzato impostato");
      try { new Audio(dataUrl).play().catch(() => {}); } catch { /* ignore */ }
    };
    reader.readAsDataURL(file);
  };

  const resetSound = () => {
    try { localStorage.removeItem("alertSound"); } catch { /* ignore */ }
    setCustomSound(null);
    toast.info("Suono ripristinato al default");
  };

  // Ring when a new alert appears (newest alert timestamp changes).
  useEffect(() => {
    if (!alerts || alerts.length === 0) return;
    const newest = alerts[0].created_at;
    if (lastAlertRef.current === null) {
      lastAlertRef.current = newest;  // baseline on first load: don't ring
      return;
    }
    if (newest !== lastAlertRef.current) {
      lastAlertRef.current = newest;
      if (soundEnabled) playAlertSound();
    }
  }, [alerts, soundEnabled, playAlertSound]);

  // Toast once when an API quota warning appears (or changes).
  useEffect(() => {
    const w = status?.quota_warning || null;
    if (w && w !== quotaWarnRef.current) {
      toast.error(`⚠️ Quota API — ${w}`, { duration: 12000 });
    }
    quotaWarnRef.current = w;
  }, [status?.quota_warning]);

  const manualRefresh = async () => {
    if (scanningRef.current) return;
    scanningRef.current = true;
    setRefreshing(true);
    try {
      const r = await axios.post(`${API}/refresh`);
      const st = r.data.stats || {};
      toast.success(`Scan · ${st.selections_tracked ?? 0} quote tracciate, ${st.drops_found ?? 0} drop`);
      await loadAll();
    } catch (e) {
      toast.error("Errore scan: " + (e?.response?.data?.detail || e.message));
    } finally {
      scanningRef.current = false;
      setRefreshing(false);
    }
  };

  const provider = status?.provider;
  const setProvider = async (p) => {
    if (!p || p === provider) return;
    try {
      await axios.put(`${API}/settings`, { provider: p });
      toast.success(`Provider: ${PROVIDER_LABELS[p] || p}`);
      await loadAll();
    } catch (e) {
      toast.error("Errore: " + (e?.response?.data?.detail || e.message));
    }
  };

  const tracking = !!status?.tracking_enabled;
  const toggleTracking = async () => {
    try {
      await axios.post(`${API}/tracking`, { enabled: !tracking });
      toast[!tracking ? "success" : "info"](`Tracciamento ${!tracking ? "attivato" : "disattivato"}`);
      await loadAll();
    } catch (e) {
      toast.error("Errore: " + (e?.response?.data?.detail || e.message));
    }
  };

  // Missing field (backend not yet redeployed) reads as ON, like before.
  const tennis = status?.tennis_enabled !== false;
  const toggleTennis = async () => {
    try {
      await axios.put(`${API}/settings`, { tennis_enabled: !tennis });
      toast[!tennis ? "success" : "info"](`Tennis ${!tennis ? "attivato" : "disattivato"}`);
      await loadAll();
    } catch (e) {
      toast.error("Errore: " + (e?.response?.data?.detail || e.message));
    }
  };

  const basketball = !!status?.basketball_enabled;
  const toggleBasketball = async () => {
    try {
      await axios.put(`${API}/settings`, { basketball_enabled: !basketball });
      toast[!basketball ? "success" : "info"](`Basket ${!basketball ? "attivato" : "disattivato"}`);
      await loadAll();
    } catch (e) {
      toast.error("Errore: " + (e?.response?.data?.detail || e.message));
    }
  };

  const football = !!status?.football_enabled;
  const toggleFootball = async () => {
    try {
      await axios.put(`${API}/settings`, { football_enabled: !football });
      toast[!football ? "success" : "info"](`Calcio ${!football ? "attivato" : "disattivato"}`);
      await loadAll();
    } catch (e) {
      toast.error("Errore: " + (e?.response?.data?.detail || e.message));
    }
  };

  const hockey = !!status?.hockey_enabled;
  const toggleHockey = async () => {
    try {
      await axios.put(`${API}/settings`, { hockey_enabled: !hockey });
      toast[!hockey ? "success" : "info"](`Hockey ${!hockey ? "attivato" : "disattivato"}`);
      await loadAll();
    } catch (e) {
      toast.error("Errore: " + (e?.response?.data?.detail || e.message));
    }
  };

  const volley = !!status?.volley_enabled;
  const toggleVolley = async () => {
    try {
      await axios.put(`${API}/settings`, { volley_enabled: !volley });
      toast[!volley ? "success" : "info"](`Volley ${!volley ? "attivato" : "disattivato"}`);
      await loadAll();
    } catch (e) {
      toast.error("Errore: " + (e?.response?.data?.detail || e.message));
    }
  };

  const f1 = !!status?.f1_enabled;
  const toggleF1 = async () => {
    try {
      await axios.put(`${API}/settings`, { f1_enabled: !f1 });
      toast[!f1 ? "success" : "info"](`Formula 1 ${!f1 ? "attivata" : "disattivata"}`);
      await loadAll();
    } catch (e) {
      toast.error("Errore: " + (e?.response?.data?.detail || e.message));
    }
  };

  const outright = !!status?.outright_enabled;
  const toggleOutright = async () => {
    try {
      await axios.put(`${API}/settings`, { outright_enabled: !outright });
      toast[!outright ? "success" : "info"](`Outright ${!outright ? "attivati" : "disattivati"}`);
      await loadAll();
    } catch (e) {
      toast.error("Errore: " + (e?.response?.data?.detail || e.message));
    }
  };

  const whale = !!status?.whale_enabled;
  const toggleWhale = async () => {
    try {
      await axios.put(`${API}/settings`, { whale_enabled: !whale });
      toast[!whale ? "success" : "info"](`Whale monitor ${!whale ? "attivato" : "disattivato"}`);
      await loadAll();
    } catch (e) {
      toast.error("Errore: " + (e?.response?.data?.detail || e.message));
    }
  };

  // [settings key, enabled, button text, display name, tooltip]
  const pmToggles = [
    ["mlb", !!status?.mlb_enabled, "⚾", "MLB", "Traccia MLB su Polymarket (mercati predittivi, gratis)"],
    ["nba_props", !!status?.nba_props_enabled, "🏀 PROPS PM", "NBA player props (Polymarket)",
      "Traccia i player props NBA su Polymarket (solo book liquidi, gratis)"],
    ["props_nba", !!status?.props_nba_enabled, "🏀 PROPS FD", "NBA player props (book)",
      "Player props NBA dei bookmaker via PropLine: alert solo se 2+ book muovono insieme. Serve la key PropLine."],
    ["props_seriea", !!status?.props_seriea_enabled, "⚽ PROPS SA", "Serie A player props",
      "Tiri in porta e tiri della Serie A via PropLine (book USA): solo Over, alert solo se 2+ book e il consenso muovono oltre la soglia props. Serve la key PropLine."],
    ["props_epl", !!status?.props_epl_enabled, "⚽ PROPS EPL", "Premier League player props",
      "Tiri in porta e tiri della Premier League via PropLine (book USA): solo Over, stessa regola multi-book. Serve la key PropLine."],
  ];
  const togglePmSport = async (key, enabled, name) => {
    try {
      await axios.put(`${API}/settings`, { [`${key}_enabled`]: !enabled });
      toast[!enabled ? "success" : "info"](`${name} ${!enabled ? "attivato" : "disattivato"}`);
      await loadAll();
    } catch (e) {
      toast.error("Errore: " + (e?.response?.data?.detail || e.message));
    }
  };

  // Panel helpers: per-key lookup of the generic toggles, active count, and
  // whether to open the sports panel by default (desktop yes, phone no).
  const pm = Object.fromEntries(pmToggles.map(([k, on, , name, tip]) => [k, { on, name, tip }]));
  const activeSports = [tennis, basketball, football, hockey, volley, f1, outright, whale,
    ...pmToggles.map(t => t[1])].filter(Boolean).length;
  const totalSports = 8 + pmToggles.length;

  const footballProvider = status?.football_provider;
  const setFootballProvider = async (p) => {
    if (!p || p === footballProvider) return;
    try {
      await axios.put(`${API}/settings`, { football_provider: p });
      toast.success(`Calcio → ${PROVIDER_LABELS[p] || p}`);
      await loadAll();
    } catch (e) {
      toast.error("Errore: " + (e?.response?.data?.detail || e.message));
    }
  };

  const nowSec = Math.floor(now / 1000);
  const rows = useMemo(() => {
    if (!snapshot?.matches) return [];
    const out = [];
    for (const m of snapshot.matches) {
      if (m.start_time && m.start_time <= nowSec) continue;  // match già iniziato
      for (const ln of m.lines || []) {
        if (onlyDrops && !ln.is_drop) continue;
        if ((ln.drop_from_open || 0) * 100 < minDrop) continue;
        out.push({ ...ln, match: m });
      }
    }
    out.sort((a, b) => (b.drop_from_open || 0) - (a.drop_from_open || 0));
    return out;
  }, [snapshot, onlyDrops, minDrop, nowSec]);

  const totalFixtures = (snapshot?.matches || []).filter(
    m => !m.start_time || m.start_time > nowSec
  ).length;
  const activeDrops = rows.filter(r => r.is_drop).length;
  const threshold = (status?.drop_threshold ?? settings?.drop_threshold ?? 0.05) * 100;

  return (
    <div className="min-h-screen lg:h-screen w-full flex flex-col bg-[#0A0A0A] text-white lg:overflow-hidden" data-testid="dashboard-root">
      <Toaster theme="dark" position="top-right" />

      {/* TOP BAR - row 1: brand + actions; row 2: live status + quotas */}
      <header className="shrink-0 border-b border-white/10 bg-[#121212]" data-testid="top-bar">
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 px-3 md:px-6 py-2.5">
          <div className="flex items-center gap-2 min-w-0">
            <div className={`w-2 h-2 rounded-full shrink-0 ${tracking ? "bg-[#32D74B] pulse-dot" : "bg-zinc-600"}`} />
            <span className="font-display uppercase tracking-tight text-lg md:text-xl font-bold truncate">Pinnacle <span className="text-[#007AFF]">Drop</span> Monitor</span>
            <span className="hidden xl:inline ml-2 text-[10px] text-zinc-500 uppercase tracking-widest font-mono">Pinnacle · Polymarket · Player props</span>
          </div>

          <div className="flex items-center gap-2">
            <button
              data-testid="tracking-toggle"
              onClick={toggleTracking}
              title={tracking ? "Tracciamento attivo (clic per sospendere tutti gli scan)" : "Tracciamento sospeso (clic per riattivare)"}
              className={`flex items-center gap-2 px-3 py-1.5 border text-xs font-bold uppercase tracking-widest transition-colors ${
                tracking
                  ? "border-[#32D74B]/40 bg-[#32D74B]/10 text-[#32D74B] hover:bg-[#32D74B]/20"
                  : "border-white/20 bg-white/5 text-zinc-400 hover:bg-white/10"
              }`}
            >
              <Power size={14} /> <span className="hidden sm:inline">Tracking</span> {tracking ? "ON" : "OFF"}
            </button>

            <button
              data-testid="sound-toggle"
              onClick={toggleSound}
              title={soundEnabled ? "Suono alert attivo (clic per disattivare)" : "Suono alert disattivato (clic per attivare)"}
              className={`p-2 border border-white/15 transition-colors hover:bg-white/5 ${soundEnabled ? "text-white" : "text-zinc-500"}`}
            >
              {soundEnabled ? <Volume2 size={14} /> : <VolumeX size={14} />}
            </button>

            {status?.use_mock_data && (
              <button
                data-testid="mock-toggle-off"
                onClick={async () => { await axios.post(`${API}/mock/false`); await loadAll(); toast.info("Live API mode"); }}
                className="text-[10px] uppercase tracking-widest text-[#FF9F0A] border border-[#FF9F0A]/40 bg-[#FF9F0A]/10 px-2 py-1.5 hover:bg-[#FF9F0A]/20"
              >
                Demo · off
              </button>
            )}

            <Button
              data-testid="refresh-btn"
              onClick={manualRefresh}
              disabled={refreshing}
              title="Esegui uno scan adesso"
              className="rounded-none bg-[#007AFF] hover:bg-[#0056b3] text-white uppercase tracking-wider text-xs font-bold px-3"
            >
              <RefreshCw size={14} className={`sm:mr-2 ${refreshing ? "animate-spin" : ""}`} />
              <span className="hidden sm:inline">{refreshing ? "Scanning" : "Refresh"}</span>
            </Button>

            <SettingsDialog
              settings={settings}
              onSaved={loadAll}
              customSound={customSound}
              onUploadSound={uploadSound}
              onResetSound={resetSound}
              onPreviewSound={playAlertSound}
            />
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 px-3 md:px-6 py-1.5 border-t border-white/5 bg-[#0E0E0E]">
          <StatusPill label="Last scan" value={status?.last_scan_at ? new Date(status.last_scan_at).toLocaleTimeString() : "—"} icon={<Clock size={13} />} />
          <StatusPill label="Next scan" value={<Countdown to={status?.next_scan_at} />} icon={<Radio size={13} className="text-[#007AFF]" />} mono />
          <StatusPill label="Drop ≥" value={`${threshold.toFixed(1)}%`} icon={<TrendingDown size={13} />} mono />
          {provider === "theoddsapi" && status?.requests_remaining != null && (
            <StatusPill label="Credits" value={status.requests_remaining} icon={<Activity size={13} />} mono />
          )}
          <QuotaPill
            label="PropLine"
            remaining={status?.propline_requests_remaining}
            limit={status?.propline_daily_limit}
            status={status?.propline_status}
            testId="propline-quota-pill"
          />
        </div>
      </header>

      {status?.quota_warning && (
        <div
          data-testid="quota-banner"
          className="shrink-0 flex items-center gap-2 px-6 py-2 bg-[#FF3B30]/10 border-b border-[#FF3B30]/40 text-[#FF3B30] text-xs uppercase tracking-widest"
        >
          <AlertCircle size={14} className="shrink-0" />
          <span>Quota API — {status.quota_warning}. Aggiorna la key o cambia provider.</span>
        </div>
      )}

      {/* MAIN */}
      <div className="flex flex-col lg:flex-row flex-1 lg:overflow-hidden">
        {/* SIDEBAR */}
        <aside className="w-full lg:w-[22rem] border-b lg:border-b-0 lg:border-r border-white/10 bg-[#0A0A0A] p-4 md:p-6 flex flex-col gap-5 lg:gap-8 lg:overflow-y-auto shrink-0" data-testid="sidebar">
          <Collapsible
            title="Sport & mercati"
            icon={<Layers size={12} />}
            defaultOpen={isDesktop}
            testId="sports-panel"
            right={<span className="font-mono normal-case tracking-normal text-zinc-400">{activeSports}/{totalSports} attivi</span>}
          >
            <GroupLabel note="Pinnacle · OddsPapi">⬇️ Drop Pinnacle</GroupLabel>
            <div className="grid grid-cols-2 gap-1">
              <SportToggle testId="tennis-toggle" emoji="🎾" label="Tennis" sub="ATP · WTA · ITF" on={tennis} onClick={toggleTennis} tone="#32D74B"
                title="Traccia il tennis (ATP/WTA/Challenger/ITF)" />
              <SportToggle testId="basketball-toggle" emoji="🏀" label="Basket" sub="NBA · EuroLeague" on={basketball} onClick={toggleBasketball} tone="#FF9F0A"
                title="Traccia il basket (NBA/WNBA/EuroLeague/EuroBasket)" />
              <SportToggle testId="football-toggle" emoji="⚽" label="Calcio" sub="Top leghe · UEFA" on={football} onClick={toggleFootball} tone="#32D74B"
                title="Traccia il calcio (campionati principali, coppe UEFA, Nations League)" />
              <SportToggle testId="hockey-toggle" emoji="🏒" label="Hockey" sub="NHL e leghe" on={hockey} onClick={toggleHockey} tone="#64D2FF"
                title="Traccia l'hockey su ghiaccio" />
              <SportToggle testId="volley-toggle" emoji="🏐" label="Volley" sub="SuperLega · CEV" on={volley} onClick={toggleVolley} tone="#FFD60A"
                title="Traccia il volley (SuperLega, Serie A1 femminile, coppe CEV, nazionali · solo H2H)" />
            </div>
            <div className="mt-2 space-y-1.5">
              <div className="flex items-center justify-between gap-2">
                <span className="text-[10px] uppercase tracking-widest text-zinc-500">Fonte tennis</span>
                <Segmented
                  testId="provider-switch"
                  itemTestId="provider"
                  options={settings?.providers || ["theoddsapi", "oddspapi"]}
                  value={provider}
                  onChange={setProvider}
                  color="#007AFF"
                />
              </div>
              {football && (
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[10px] uppercase tracking-widest text-zinc-500">Fonte calcio</span>
                  <Segmented
                    testId="football-provider-switch"
                    itemTestId="football-provider"
                    options={settings?.providers || ["theoddsapi", "oddspapi"]}
                    value={footballProvider}
                    onChange={setFootballProvider}
                    color="#32D74B"
                  />
                </div>
              )}
            </div>

            <GroupLabel note="Polymarket · gratis">📉 Prediction markets</GroupLabel>
            <div className="grid grid-cols-2 gap-1">
              <SportToggle testId="f1-toggle" emoji="🏎️" label="Formula 1" sub="gara · pole · sprint" on={f1} onClick={toggleF1} tone="#FF2D55"
                title="Formula 1 su Polymarket (vincitore, podio, pole, sprint) con cross-check Kalshi" />
              <SportToggle testId="mlb-toggle" emoji="⚾" label="MLB" sub="moneyline" on={pm.mlb.on} onClick={() => togglePmSport("mlb", pm.mlb.on, pm.mlb.name)} tone="#FF2D55"
                title={pm.mlb.tip} />
              <SportToggle testId="nba_props-toggle" emoji="🏀" label="Props NBA" sub="Polymarket" on={pm.nba_props.on} onClick={() => togglePmSport("nba_props", pm.nba_props.on, pm.nba_props.name)} tone="#FF2D55"
                title={pm.nba_props.tip} />
              <SportToggle testId="outright-toggle" emoji="🏆" label="Outright" sub="vincitori torneo" on={outright} onClick={toggleOutright} tone="#FFD60A"
                title="Outright su Polymarket (vincitore CL/campionati, Pallone d'Oro, Slam, golf) dall'apertura alla chiusura" />
              <SportToggle testId="whale-toggle" emoji="🐋" label="Whale" sub="ordini grandi" on={whale} onClick={toggleWhale} tone="#0A84FF"
                title="Grandi ordini su Polymarket sui mercati degli sport monitorati" />
            </div>

            <GroupLabel note="PropLine · multi-book">📈 Player props</GroupLabel>
            <div className="grid grid-cols-2 gap-1">
              <SportToggle testId="props_nba-toggle" emoji="🏀" label="NBA" sub="FanDuel + book" on={pm.props_nba.on} onClick={() => togglePmSport("props_nba", pm.props_nba.on, pm.props_nba.name)} tone="#BF5AF2"
                title={pm.props_nba.tip} />
              <SportToggle testId="props_seriea-toggle" emoji="⚽" label="Serie A" sub="tiri · tiri in porta" on={pm.props_seriea.on} onClick={() => togglePmSport("props_seriea", pm.props_seriea.on, pm.props_seriea.name)} tone="#BF5AF2"
                title={pm.props_seriea.tip} />
              <SportToggle testId="props_epl-toggle" emoji="⚽" label="Premier" sub="tiri · tiri in porta" on={pm.props_epl.on} onClick={() => togglePmSport("props_epl", pm.props_epl.on, pm.props_epl.name)} tone="#BF5AF2"
                title={pm.props_epl.tip} />
            </div>
            <div className="mt-2">
              <QuotaMeter
                remaining={status?.propline_requests_remaining}
                limit={status?.propline_daily_limit}
                status={status?.propline_status}
                checkedAt={status?.propline_checked_at}
              />
            </div>
          </Collapsible>

          <div>
          <SectionTitle icon={<Activity size={12} />}>Session</SectionTitle>
          <div className="grid grid-cols-4 lg:grid-cols-2 gap-1 -mt-2 lg:mt-0">
            <StatBox label="Matches" value={totalFixtures} testId="stat-matches" />
            <StatBox label="Drops now" value={activeDrops} accent testId="stat-drops" />
            <StatBox label="Alerts sent" value={status?.last_scan_stats?.alerts_sent ?? 0} testId="stat-alerts" />
            <StatBox label="Tracked" value={status?.last_scan_stats?.selections_tracked ?? 0} testId="stat-tracked" />
          </div>
          </div>

          <Collapsible title="Filters" icon={<Filter size={12} />} defaultOpen={isDesktop} testId="filters-panel">
            <div className="space-y-6 mt-3">
              <div className="flex items-center justify-between">
                <Label className="text-xs uppercase tracking-widest text-zinc-400">Only drops</Label>
                <Switch data-testid="filter-only-drops" checked={onlyDrops} onCheckedChange={setOnlyDrops} />
              </div>
              <div>
                <div className="flex justify-between text-xs uppercase tracking-widest text-zinc-400 mb-2">
                  <span>Min drop shown</span>
                  <span className="font-mono text-white">{minDrop.toFixed(1)}%</span>
                </div>
                <Slider
                  data-testid="filter-min-drop"
                  min={0} max={20} step={0.5}
                  value={[minDrop]}
                  onValueChange={(v) => setMinDrop(v[0])}
                />
              </div>
            </div>
          </Collapsible>

          {status?.last_scan_error && (
            <div className="border border-[#FF3B30]/40 bg-[#FF3B30]/5 p-3 text-xs">
              <div className="flex items-center gap-2 text-[#FF3B30] font-semibold uppercase tracking-widest">
                <AlertCircle size={12} /> Scan error
              </div>
              <div className="mt-1 text-zinc-300 break-words">{status.last_scan_error}</div>
            </div>
          )}
        </aside>

        {/* CONTENT */}
        <main className="flex-1 flex flex-col lg:overflow-hidden">
          <section className="flex-1 overflow-x-auto lg:overflow-auto border-b border-white/10" data-testid="lines-table-section">
            {/* Mobile: card list (the wide table is unreadable on a phone) */}
            <div className="md:hidden" data-testid="lines-cards">
              {rows.length === 0 ? (
                <div className="p-8 text-center text-zinc-500 text-sm">
                  <div className="font-display uppercase tracking-wider text-xl mb-2 text-zinc-700">
                    {tracking ? "Nessun calo rilevante" : "Tracciamento disattivato"}
                  </div>
                  <div>
                    {tracking
                      ? <>Prossimo controllo tra <span className="text-white"><Countdown to={status?.next_scan_at} /></span>.</>
                      : "Attiva il tracciamento dal pulsante in alto."}
                  </div>
                </div>
              ) : (
                rows.map((r, i) => (
                  <div key={i} className={`border-b border-white/5 p-3 ${r.is_drop ? "bg-[#32D74B]/5" : ""}`} data-testid={`line-card-${i}`}>
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="text-white text-sm truncate">
                          {r.match.player2 ? <>{r.match.player1} <span className="text-zinc-600">vs</span> {r.match.player2}</> : (r.match.player1 || r.match.tournament)}
                        </div>
                        <div className="text-[10px] text-zinc-500 uppercase tracking-widest truncate">{r.match.sport_emoji}{r.match.player1 ? ` ${r.match.tournament}` : ""}</div>
                      </div>
                      <div className="shrink-0 text-right">
                        <DropBadge drop={r.drop_from_open} isDrop={r.is_drop} />
                        {r.suspect_live && (
                          <div className="text-[9px] uppercase tracking-widest text-[#FF3B30]" title="Possibile match in corso: alert sospesi">live?</div>
                        )}
                      </div>
                    </div>
                    <div className="mt-2 flex items-center justify-between text-xs font-mono">
                      <span className="text-zinc-300"><span className="text-zinc-500 uppercase tracking-widest text-[10px]">{r.market_name} · </span>{r.label}</span>
                      <span className="text-zinc-500">{r.match.start_time ? `${fmtTime(r.match.start_time)} · in ${minsUntil(r.match.start_time)}m` : "outright · mercato aperto"}</span>
                    </div>
                    <div className="mt-1 flex items-center gap-4 text-xs font-mono">
                      <span className="text-zinc-500">Open <span className="text-zinc-300">{r.open_price?.toFixed(2)}</span></span>
                      <span className="text-zinc-500">Pinnacle <span className="text-white font-semibold">{r.price?.toFixed(2)}</span></span>
                    </div>
                  </div>
                ))
              )}
            </div>
            {/* Desktop: full table */}
            <table className="hidden md:table w-full text-sm text-left font-mono whitespace-nowrap">
              <thead className="sticky top-0 z-10 bg-[#121212] border-b border-white/20 text-zinc-500 text-[10px] uppercase tracking-widest">
                <tr>
                  <Th>Start</Th>
                  <Th>Match</Th>
                  <Th>Market</Th>
                  <Th>Selection</Th>
                  <Th className="text-right">Open</Th>
                  <Th className="text-right">Pinnacle</Th>
                  <Th className="text-right">Drop</Th>
                </tr>
              </thead>
              <tbody data-testid="lines-table-body">
                {rows.length === 0 && (
                  <tr>
                    <td colSpan={7} className="p-16 text-center text-zinc-500 text-sm">
                      <div className="font-display uppercase tracking-wider text-2xl mb-2 text-zinc-700">
                        {tracking ? "Nessun calo rilevante" : "Tracciamento disattivato"}
                      </div>
                      <div>
                        {tracking
                          ? <>Monitoro i match con inizio nei prossimi 60 minuti. Prossimo controllo tra <span className="text-white"><Countdown to={status?.next_scan_at} /></span>.</>
                          : "Attiva il tracciamento dal pulsante in alto per iniziare a monitorare le quote."}
                      </div>
                    </td>
                  </tr>
                )}
                {rows.map((r, i) => (
                  <tr key={i} className={`border-b border-white/5 hover:bg-white/5 transition-colors ${r.is_drop ? "bg-[#32D74B]/5" : ""}`} data-testid={`line-row-${i}`}>
                    <Td className="text-zinc-400">
                      <div className="flex flex-col leading-tight">
                        <span>{r.match.start_time ? fmtTime(r.match.start_time) : "aperto"}</span>
                        {r.match.start_time
                          ? <span className="text-[10px] text-zinc-600">in {minsUntil(r.match.start_time)}m</span>
                          : <span className="text-[10px] text-zinc-600">outright</span>}
                      </div>
                    </Td>
                    <Td>
                      <div className="flex flex-col leading-tight">
                        <span className="text-white">
                          {r.match.player2 ? <>{r.match.player1} <span className="text-zinc-600">vs</span> {r.match.player2}</> : (r.match.player1 || r.match.tournament)}
                        </span>
                        <span className="text-[10px] text-zinc-500 uppercase tracking-widest">{r.match.sport_emoji}{r.match.player1 ? ` ${r.match.tournament}` : ""}</span>
                      </div>
                    </Td>
                    <Td className="text-zinc-400 text-xs uppercase tracking-widest">{r.market_name}</Td>
                    <Td className="text-white">{r.label}</Td>
                    <Td className="text-right text-zinc-500">{r.open_price?.toFixed(2)}</Td>
                    <Td className="text-right text-white font-semibold">{r.price?.toFixed(2)}</Td>
                    <Td className="text-right">
                      <DropBadge drop={r.drop_from_open} isDrop={r.is_drop} />
                      {r.suspect_live && (
                        <span className="ml-1 text-[9px] uppercase tracking-widest text-[#FF3B30]" title="Possibile match in corso: alert sospesi">live?</span>
                      )}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          {/* ALERT LOG */}
          <section className="h-64 bg-[#0A0A0A] p-4 overflow-y-auto font-mono text-xs shrink-0" data-testid="alert-log">
            <div className="flex items-center justify-between mb-2 sticky top-0 bg-[#0A0A0A] pb-2 border-b border-white/10">
              <div className="flex items-center gap-2 text-zinc-500 uppercase tracking-widest text-[10px]">
                <Send size={12} /> Drop alert log
              </div>
              <div className="flex items-center gap-3">
                <button
                  data-testid="toggle-hide-started"
                  onClick={() => setHideStarted(v => !v)}
                  className={`text-[10px] uppercase tracking-widest border px-2 py-1 transition-colors ${
                    hideStarted ? "border-[#32D74B]/40 bg-[#32D74B]/10 text-[#32D74B]" : "border-white/15 text-zinc-500 hover:bg-white/5"
                  }`}
                >
                  Nascondi iniziati
                </button>
                <span className="text-zinc-600">{alerts.length} events</span>
              </div>
            </div>
            {alerts.length === 0 && (
              <div className="text-zinc-600 py-6 text-center">Nessun alert ancora. I cali di quota Pinnacle verranno stampati qui.</div>
            )}
            {alerts.filter(a => !(hideStarted && isUnderway(a, now))).map((a, i) => {
              const underway = isUnderway(a, now);
              if (a.type === "whale") {
                const buy = a.whale_side === "BUY";
                return (
                <div key={`${i}-${a.created_at}-${a.label}`} className="py-1 border-b border-white/5 flex items-start gap-2 md:gap-3" data-testid="alert-row">
                  <span className="text-zinc-600 shrink-0 whitespace-nowrap">{new Date(a.created_at).toLocaleTimeString()}</span>
                  {a.telegram_ok ? <CheckCircle2 size={12} className="text-[#32D74B] shrink-0 mt-0.5" /> : <XCircle size={12} className="text-[#FF3B30] shrink-0 mt-0.5" />}
                  <span className="text-zinc-300 min-w-0 break-words">
                    <span className="mr-1">🐋</span>
                    <span className="text-[#0A84FF]">{a.tournament}</span>
                    <span className="text-white"> {a.player1}</span>
                    <span className="text-zinc-600"> · </span>
                    <span className={buy ? "text-[#32D74B]" : "text-[#FF3B30]"}>{buy ? "COMPRA" : "VENDE"} ${a.whale_usd?.toLocaleString()}</span>
                    <span className="text-zinc-600"> su </span>
                    <span className="text-white">{a.label}</span>
                    <span className="text-zinc-600"> @ {a.price?.toFixed(3)}</span>
                  </span>
                </div>
                );
              }
              return (
              <div key={`${i}-${a.created_at}-${a.label}`} className={`py-1 border-b border-white/5 flex items-start gap-2 md:gap-3 ${underway ? "opacity-50" : ""}`} data-testid="alert-row">
                <span className="text-zinc-600 shrink-0 whitespace-nowrap">{new Date(a.created_at).toLocaleTimeString()}</span>
                {a.telegram_ok ? <CheckCircle2 size={12} className="text-[#32D74B] shrink-0 mt-0.5" /> : <XCircle size={12} className="text-[#FF3B30] shrink-0 mt-0.5" />}
                <span className="text-zinc-300 min-w-0 break-words">
                  {underway && <span title="Match già iniziato" className="mr-1 text-zinc-500">▶</span>}
                  {SPORT_EMOJI[a.sport] ? `${SPORT_EMOJI[a.sport]} ` : ""}
                  {(() => {
                    // Source pill, same colours as the sidebar groups.
                    const src = SOURCE_PILL[a.provider] || SOURCE_PILL.default;
                    return (
                      <span
                        className="mr-1 px-1 py-0.5 text-[8px] uppercase tracking-widest border align-middle"
                        style={{ color: src.color, borderColor: `${src.color}66` }}
                        title={src.title}
                      >
                        {src.label}
                      </span>
                    );
                  })()}
                  <span className="text-white">{a.player2 ? `${a.player1} vs ${a.player2}` : (a.player1 || a.tournament)}</span>
                  <span className="text-zinc-600"> · {a.market_name} — </span>
                  <span className="text-white">{a.label}</span>
                  <span className="text-zinc-600"> · </span>
                  {a.prev_price?.toFixed(2)} → <span className="text-white">{a.price?.toFixed(2)}</span>
                  <span className="ml-2 text-[#32D74B]">-{(a.drop_last * 100).toFixed(1)}%</span>
                  {a.best_book_it && a.best_price_it != null && (
                    <span className="ml-2 text-[#64D2FF]">🇮🇹 {a.best_book_it} {a.best_price_it.toFixed(2)}</span>
                  )}
                  {a.betfair_price != null && (
                    <span className="ml-2 text-[#FFD60A]">BF {a.betfair_price.toFixed(2)}</span>
                  )}
                  {a.polymarket_price != null && (
                    <span className="ml-2 text-[#BF5AF2]">PM {a.polymarket_price.toFixed(2)}</span>
                  )}
                  {a.kalshi_price != null && (
                    <span className="ml-2 text-[#FF9F0A]">KS {a.kalshi_price.toFixed(2)}</span>
                  )}
                </span>
              </div>
              );
            })}
          </section>
        </main>
      </div>
    </div>
  );
}

// Self-ticking countdown to an ISO timestamp: only this text node re-renders
// every second, not the whole dashboard.
function Countdown({ to }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const sec = to ? Math.max(0, Math.floor((new Date(to).getTime() - now) / 1000)) : null;
  return <>{fmtCountdown(sec)}</>;
}

// Collapsible sidebar section (the sports panel starts closed on phones so the
// odds table is reachable without scrolling past a dozen toggles).
function Collapsible({ title, icon, defaultOpen = true, right, testId, children }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div data-testid={testId}>
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        className="w-full flex items-center justify-between gap-2 text-[10px] uppercase tracking-[0.2em] font-semibold text-zinc-500 hover:text-zinc-300"
      >
        <span className="flex items-center gap-2">{icon} {title}</span>
        <span className="flex items-center gap-2">
          {right}
          <ChevronDown size={12} className={`transition-transform ${open ? "rotate-180" : ""}`} />
        </span>
      </button>
      {open && <div className="mt-1">{children}</div>}
    </div>
  );
}

function GroupLabel({ children, note }) {
  return (
    <div className="flex items-baseline justify-between gap-2 mt-4 mb-1.5">
      <span className="text-[10px] uppercase tracking-widest text-zinc-300 font-semibold">{children}</span>
      <span className="text-[9px] uppercase tracking-widest text-zinc-600">{note}</span>
    </div>
  );
}

// One sport/market on-off control: emoji + name + scope, with a small switch.
function SportToggle({ emoji, label, sub, on, onClick, title, testId, tone = "#32D74B" }) {
  return (
    <button
      type="button"
      data-testid={testId}
      onClick={onClick}
      title={title}
      aria-pressed={!!on}
      className={`flex items-center justify-between gap-2 w-full px-2 py-1.5 border text-left transition-colors ${
        on ? "border-white/15 bg-white/[0.04] hover:bg-white/[0.08]" : "border-white/10 hover:bg-white/[0.04]"
      }`}
    >
      <span className="flex items-center gap-2 min-w-0">
        <span className={`text-sm leading-none ${on ? "" : "grayscale opacity-40"}`}>{emoji}</span>
        <span className="min-w-0">
          <span className={`block text-[11px] font-semibold uppercase tracking-wide truncate ${on ? "text-white" : "text-zinc-500"}`}>{label}</span>
          {sub && <span className="block text-[9px] text-zinc-600 truncate">{sub}</span>}
        </span>
      </span>
      <span
        className="relative inline-block h-3.5 w-6 shrink-0 border transition-colors"
        style={{ borderColor: on ? tone : "rgba(255,255,255,0.2)", background: on ? `${tone}33` : "transparent" }}
      >
        <span
          className="absolute top-[2px] h-2 w-2 transition-all"
          style={{ left: on ? 12 : 2, background: on ? tone : "#52525B" }}
        />
      </span>
    </button>
  );
}

// Two-way segmented switch (tennis / football odds provider).
function Segmented({ options, value, onChange, testId, itemTestId, color }) {
  return (
    <div className="flex border border-white/15" data-testid={testId}>
      {options.map(p => (
        <button
          key={p}
          type="button"
          data-testid={`${itemTestId}-${p}`}
          onClick={() => onChange(p)}
          className={`px-2 py-1 text-[9px] uppercase tracking-widest font-bold transition-colors ${
            value === p ? "text-black" : "text-zinc-400 hover:bg-white/5"
          }`}
          style={value === p ? { background: color } : undefined}
        >
          {PROVIDER_LABELS[p] || p}
        </button>
      ))}
    </div>
  );
}

function quotaColor(remaining, limit) {
  if (remaining == null) return "#71717A";
  const pct = remaining / (limit || 1000);
  return pct > 0.3 ? "#32D74B" : pct > 0.1 ? "#FF9F0A" : "#FF3B30";
}

// Compact top-bar counter of the PropLine requests left today.
function QuotaPill({ label, remaining, limit, status, testId }) {
  if (remaining == null && !status) return null;
  const bad = (status || "").startsWith("✗");
  const color = bad ? "#FF3B30" : quotaColor(remaining, limit);
  return (
    <div className="flex items-center gap-2 text-xs" data-testid={testId} title={status || ""}>
      <span className="inline-block w-1.5 h-1.5 rounded-full" style={{ background: color }} />
      <span className="text-zinc-500 uppercase tracking-widest text-[10px]">{label}</span>
      <span className="font-mono" style={{ color }}>
        {remaining ?? "—"}<span className="text-zinc-600">/{limit || 1000}</span>
      </span>
    </div>
  );
}

// Sidebar meter: PropLine daily quota with a bar, reset time and key status.
function QuotaMeter({ remaining, limit, status, checkedAt }) {
  if (remaining == null && !status) {
    return (
      <div className="border border-white/10 p-2.5 text-[10px] text-zinc-600" data-testid="propline-quota">
        PropLine: imposta la key in Settings per i player props dei book.
      </div>
    );
  }
  const lim = limit || 1000;
  const pct = remaining != null ? Math.max(0, Math.min(100, (remaining / lim) * 100)) : 0;
  const bad = (status || "").startsWith("✗");
  const color = bad ? "#FF3B30" : quotaColor(remaining, lim);
  return (
    <div className="border border-white/10 p-2.5" data-testid="propline-quota">
      <div className="flex items-baseline justify-between text-[10px] uppercase tracking-widest text-zinc-500">
        <span>Chiamate PropLine oggi</span>
        <span className="font-mono text-sm" style={{ color }}>
          {remaining ?? "—"}<span className="text-zinc-600 text-[10px]"> / {lim.toLocaleString()}</span>
        </span>
      </div>
      <div className="mt-1.5 h-1 bg-white/10">
        <div className="h-1 transition-all" style={{ width: `${pct}%`, background: color }} />
      </div>
      {bad && <div className="mt-1.5 text-[10px] text-[#FF3B30]">{status}</div>}
      <div className="mt-1 text-[10px] text-zinc-600">
        Reset alle 00:00 UTC{checkedAt ? ` · agg. ${new Date(checkedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : ""}
      </div>
    </div>
  );
}

function SectionTitle({ children, icon }) {
  return (
    <div className="text-[10px] uppercase tracking-[0.2em] font-semibold text-zinc-500 mb-4 flex items-center gap-2">
      {icon} {children}
    </div>
  );
}
function StatusPill({ label, value, icon, mono }) {
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="text-zinc-500">{icon}</span>
      <span className="text-zinc-500 uppercase tracking-widest text-[10px]">{label}</span>
      <span className={`text-white ${mono ? "font-mono" : ""}`}>{value}</span>
    </div>
  );
}
function StatBox({ label, value, accent, testId }) {
  return (
    <div className="border border-white/10 p-2 lg:p-3 bg-[#0A0A0A] min-w-0" data-testid={testId}>
      <div className="text-[9px] lg:text-[10px] uppercase tracking-wider lg:tracking-widest text-zinc-500 truncate">{label}</div>
      <div className={`font-display text-xl lg:text-3xl font-bold tracking-tight ${accent ? "text-[#32D74B]" : "text-white"}`}>{value}</div>
    </div>
  );
}
function Th({ children, className = "" }) {
  return <th className={`px-4 py-2.5 font-semibold ${className}`}>{children}</th>;
}
function Td({ children, className = "" }) {
  return <td className={`px-4 py-2.5 ${className}`}>{children}</td>;
}
function DropBadge({ drop, isDrop }) {
  const pct = ((drop || 0) * 100).toFixed(1);
  const cls = isDrop
    ? "bg-[#32D74B]/10 text-[#32D74B] border-[#32D74B]/30"
    : (drop || 0) > 0
      ? "bg-white/5 text-zinc-300 border-white/10"
      : "bg-white/5 text-zinc-600 border-white/10";
  return (
    <span className={`inline-block border px-1.5 py-0.5 text-xs font-mono ${cls}`} data-testid="drop-badge">
      -{pct}%
    </span>
  );
}

function SettingsDialog({ settings, onSaved, customSound, onUploadSound, onResetSound, onPreviewSound }) {
  const [open, setOpen] = useState(false);
  const [drop, setDrop] = useState(5);
  const [footballDrop, setFootballDrop] = useState(5);
  const [propsDrop, setPropsDrop] = useState(8);
  const [token, setToken] = useState("");
  const [chatId, setChatId] = useState("");
  const [oddspapiKey, setOddspapiKey] = useState("");
  const [theoddsapiKey, setTheoddsapiKey] = useState("");
  const [proplineKey, setProplineKey] = useState("");
  const [checkingPl, setCheckingPl] = useState(false);
  const checkPropline = async () => {
    setCheckingPl(true);
    try {
      const r = await axios.post(`${API}/propline/check`);
      if (r.data.ok) toast.success(`PropLine: key valida · ${r.data.remaining ?? "?"} richieste rimaste oggi`);
      else toast.error(`PropLine: ${r.data.error}`, { duration: 10000 });
      await onSaved();
    } catch (e) {
      toast.error("Errore: " + (e?.response?.data?.detail || e.message));
    } finally {
      setCheckingPl(false);
    }
  };
  const [whaleUsd, setWhaleUsd] = useState(25000);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (settings && open) {
      setDrop(Math.round((settings.drop_threshold ?? 0.05) * 1000) / 10);
      setFootballDrop(Math.round((settings.football_drop_threshold ?? 0.05) * 1000) / 10);
      setPropsDrop(Math.round((settings.props_drop_threshold ?? 0.08) * 1000) / 10);
      setWhaleUsd(Math.round(settings.whale_min_usd ?? 25000));
    }
  }, [settings, open]);

  const save = async () => {
    setSaving(true);
    try {
      const body = { drop_threshold: drop / 100, football_drop_threshold: footballDrop / 100, props_drop_threshold: propsDrop / 100 };
      if (Number.isFinite(whaleUsd) && whaleUsd > 0) body.whale_min_usd = whaleUsd;
      if (token) body.telegram_token = token;
      if (chatId) body.telegram_chat_id = chatId;
      if (oddspapiKey.trim()) body.oddspapi_api_key = oddspapiKey.trim();
      if (theoddsapiKey.trim()) body.theoddsapi_api_key = theoddsapiKey.trim();
      if (proplineKey.trim()) body.propline_api_key = proplineKey.trim();
      const res = await axios.put(`${API}/settings`, body);
      toast.success("Impostazioni salvate");
      const pl = res?.data?.propline_status;
      if (body.propline_api_key && pl) {
        (pl.startsWith("✓") ? toast.success : toast.error)(`PropLine: ${pl}`, { duration: 10000 });
      }
      setToken(""); setChatId(""); setOddspapiKey(""); setTheoddsapiKey(""); setProplineKey("");
      await onSaved();
      setOpen(false);
    } catch (e) {
      toast.error("Errore: " + (e?.response?.data?.detail || e.message));
    } finally {
      setSaving(false);
    }
  };

  const testTelegram = async () => {
    try {
      const r = await axios.post(`${API}/telegram/test`);
      if (r.data.ok) toast.success("Messaggio Telegram inviato ✓");
      else toast.error("Telegram error: " + JSON.stringify(r.data.response));
    } catch (e) {
      toast.error("Errore: " + e.message);
    }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button data-testid="settings-btn" variant="outline" className="rounded-none border-white/20 hover:bg-white/5 text-white uppercase tracking-wider text-xs font-bold px-4">
          <Settings size={14} className="mr-2" /> Settings
        </Button>
      </DialogTrigger>
      <DialogContent className="bg-[#0A0A0A] border-white/10 rounded-none max-w-lg w-[calc(100%-1.5rem)] max-h-[90dvh] overflow-y-auto" data-testid="settings-dialog">
        <DialogHeader>
          <DialogTitle className="font-display uppercase text-2xl tracking-tight">Monitor Settings</DialogTitle>
        </DialogHeader>

        <div className="space-y-6 pt-2">
          <div>
            <div className="flex justify-between text-xs uppercase tracking-widest text-zinc-400 mb-2">
              <span>Drop threshold</span>
              <span className="font-mono text-white">{drop.toFixed(1)}%</span>
            </div>
            <Slider data-testid="settings-drop-slider" min={1} max={15} step={0.5} value={[drop]} onValueChange={(v) => setDrop(v[0])} />
            <div className="text-[10px] text-zinc-600 mt-2">Alert quando una quota Pinnacle cala di almeno questa % tra due scansioni (tennis e basket).</div>
          </div>

          <div>
            <div className="flex justify-between text-xs uppercase tracking-widest text-zinc-400 mb-2">
              <span>⚽ Drop threshold calcio</span>
              <span className="font-mono text-white">{footballDrop.toFixed(1)}%</span>
            </div>
            <Slider data-testid="settings-football-drop-slider" min={1} max={15} step={0.5} value={[footballDrop]} onValueChange={(v) => setFootballDrop(v[0])} />
            <div className="text-[10px] text-zinc-600 mt-2">Soglia separata per il calcio: alzala se arrivano troppi alert.</div>
          </div>

          <div>
            <div className="flex justify-between text-xs uppercase tracking-widest text-zinc-400 mb-2">
              <span>🎯 Drop threshold props</span>
              <span className="font-mono text-white">{propsDrop.toFixed(1)}%</span>
            </div>
            <Slider data-testid="settings-props-drop-slider" min={2} max={20} step={0.5} value={[propsDrop]} onValueChange={(v) => setPropsDrop(v[0])} />
            <div className="text-[10px] text-zinc-600 mt-2">Player props dei book (NBA, Serie A, Premier): l'alert parte solo se almeno 2 book E il consenso di tutti i book scendono di questa percentuale. Solo Over (gli Under non sono quotati sui book italiani).</div>
          </div>

          <div className="space-y-2">
            <div className="flex justify-between text-xs uppercase tracking-widest text-zinc-400">
              <span>🐋 Soglia whale</span>
              <span className="font-mono text-white">${whaleUsd.toLocaleString()}</span>
            </div>
            <Input
              type="number"
              min={1000}
              step={1000}
              data-testid="settings-whale-usd"
              value={whaleUsd}
              onChange={e => setWhaleUsd(Number(e.target.value))}
              className="rounded-none bg-[#0A0A0A] border-white/20 font-mono"
            />
            <div className="text-[10px] text-zinc-600">Ordine minimo (in $) su Polymarket per far scattare un alert whale. Alza il valore se arrivano troppi alert.</div>
          </div>

          <div className="space-y-2">
            <div className="text-xs uppercase tracking-widest text-zinc-400">Suono alert</div>
            <div className="text-[10px] text-zinc-600">Carica un file audio breve (max ~1 MB). Vuoto = suono di default.</div>
            <input
              type="file"
              accept="audio/*"
              data-testid="settings-sound-file"
              onChange={(e) => { onUploadSound(e.target.files && e.target.files[0]); e.target.value = ""; }}
              className="block w-full text-xs text-zinc-400 file:mr-3 file:border file:border-white/20 file:bg-white/5 file:text-white file:px-3 file:py-1 file:text-xs file:uppercase file:tracking-widest hover:file:bg-white/10"
            />
            <div className="flex gap-2">
              <Button data-testid="settings-sound-preview" onClick={onPreviewSound} variant="outline" className="rounded-none border-white/20 text-white text-xs uppercase tracking-widest">
                Prova
              </Button>
              {customSound && (
                <Button data-testid="settings-sound-reset" onClick={onResetSound} variant="outline" className="rounded-none border-white/20 text-zinc-400 text-xs uppercase tracking-widest">
                  Ripristina default
                </Button>
              )}
            </div>
            {customSound && (
              <div className="text-[10px] text-[#32D74B] uppercase tracking-widest">✓ Suono personalizzato attivo</div>
            )}
          </div>

          <div className="space-y-2">
            <div className="text-xs uppercase tracking-widest text-zinc-400">OddsPapi key</div>
            <div className="text-[10px] text-zinc-600">Cambia la key OddsPapi senza toccare Google Cloud. Si applica subito e resta salvata. Lascia vuoto per non cambiare.</div>
            <Input
              data-testid="settings-oddspapi-key"
              value={oddspapiKey}
              onChange={e => setOddspapiKey(e.target.value)}
              placeholder="es. 1a2df34f-ba19-426b-b5e2-..."
              className="rounded-none bg-[#0A0A0A] border-white/20 font-mono"
            />
            {settings?.oddspapi_key_hint && (
              <div className="text-[10px] text-[#32D74B] uppercase tracking-widest">✓ Key attiva: {settings.oddspapi_key_hint}</div>
            )}
          </div>

          <div className="space-y-2">
            <div className="text-xs uppercase tracking-widest text-zinc-400">The Odds API key</div>
            <div className="text-[10px] text-zinc-600">Usata per tennis/calcio se selezioni "The Odds API" e per il cross-check miglior quota ITA + Betfair sul calcio. Lascia vuoto per non cambiare.</div>
            <Input
              data-testid="settings-theoddsapi-key"
              value={theoddsapiKey}
              onChange={e => setTheoddsapiKey(e.target.value)}
              placeholder="es. a1b2c3d4e5f6..."
              className="rounded-none bg-[#0A0A0A] border-white/20 font-mono"
            />
            {settings?.theoddsapi_key_hint && (
              <div className="text-[10px] text-[#32D74B] uppercase tracking-widest">✓ Key attiva: {settings.theoddsapi_key_hint}</div>
            )}
          </div>

          <div className="space-y-2">
            <div className="text-xs uppercase tracking-widest text-zinc-400">PropLine key</div>
            <div className="text-[10px] text-zinc-600">Player props NBA dei bookmaker (FanDuel + confronto). Key gratuita su prop-line.com (1.000 richieste/giorno). Incolla la key (va bene anche l'intero comando "curl" della mail). Lascia vuoto per non cambiare.</div>
            <Input
              data-testid="settings-propline-key"
              value={proplineKey}
              onChange={e => setProplineKey(e.target.value)}
              placeholder="es. 99c1393f8c92..."
              className="rounded-none bg-[#0A0A0A] border-white/20 font-mono"
            />
            {settings?.propline_key_hint && (
              <div className="flex items-center justify-between gap-2">
                <div className={`text-[10px] uppercase tracking-widest ${
                  (settings.propline_status || "").startsWith("✗") ? "text-[#FF3B30]" : "text-[#32D74B]"
                }`}>
                  Key {settings.propline_key_hint}{settings.propline_status ? ` — ${settings.propline_status}` : ""}
                </div>
                <button
                  type="button"
                  data-testid="settings-propline-check"
                  disabled={checkingPl}
                  onClick={checkPropline}
                  className="shrink-0 px-2 py-1 border border-white/20 text-[10px] uppercase tracking-widest text-zinc-300 hover:bg-white/10"
                >
                  {checkingPl ? "..." : "Verifica"}
                </button>
              </div>
            )}
          </div>

          <div className="space-y-2">
            <div className="text-xs uppercase tracking-widest text-zinc-400">Telegram (opzionale — lascia vuoto per non cambiare)</div>
            <div>
              <Label className="text-xs text-zinc-500">Bot Token</Label>
              <Input data-testid="settings-tg-token" value={token} onChange={e => setToken(e.target.value)} placeholder="123456:AAA..." className="rounded-none bg-[#0A0A0A] border-white/20" />
            </div>
            <div>
              <Label className="text-xs text-zinc-500">Chat ID</Label>
              <Input data-testid="settings-tg-chatid" value={chatId} onChange={e => setChatId(e.target.value)} placeholder="123456789" className="rounded-none bg-[#0A0A0A] border-white/20" />
            </div>
            <Button data-testid="settings-tg-test" onClick={testTelegram} variant="outline" className="rounded-none border-white/20 text-white text-xs uppercase tracking-widest">
              <Send size={12} className="mr-2" /> Send test message
            </Button>
            {settings?.telegram_configured && (
              <div className="text-[10px] text-[#32D74B] uppercase tracking-widest">✓ Telegram configurato</div>
            )}
          </div>
        </div>

        <DialogFooter>
          <Button data-testid="settings-save" onClick={save} disabled={saving} className="rounded-none bg-[#007AFF] hover:bg-[#0056b3] text-white uppercase tracking-wider text-xs font-bold">
            {saving ? "Saving…" : "Save settings"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

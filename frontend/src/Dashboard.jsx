import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import axios from "axios";
import { Toaster, toast } from "sonner";
import {
  RefreshCw, Settings, Send, Clock, Radio, TrendingDown,
  AlertCircle, CheckCircle2, XCircle, Activity, Filter, Power,
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
const SPORT_EMOJI = { tennis: "🎾", basketball: "🏀" };

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
  const [now, setNow] = useState(Date.now());
  const scanningRef = useRef(false);

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

  useEffect(() => { loadAll(); }, [loadAll]);
  useEffect(() => {
    const t = setInterval(loadAll, 15000);
    return () => clearInterval(t);
  }, [loadAll]);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

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

  const nextScanSec = useMemo(() => {
    if (!status?.next_scan_at) return null;
    return Math.max(0, Math.floor((new Date(status.next_scan_at).getTime() - now) / 1000));
  }, [status, now]);

  const rows = useMemo(() => {
    if (!snapshot?.matches) return [];
    const out = [];
    for (const m of snapshot.matches) {
      for (const ln of m.lines || []) {
        if (onlyDrops && !ln.is_drop) continue;
        if ((ln.drop_from_open || 0) * 100 < minDrop) continue;
        out.push({ ...ln, match: m });
      }
    }
    out.sort((a, b) => (b.drop_from_open || 0) - (a.drop_from_open || 0));
    return out;
  }, [snapshot, onlyDrops, minDrop]);

  const totalFixtures = snapshot?.matches?.length || 0;
  const activeDrops = rows.filter(r => r.is_drop).length;
  const threshold = (settings?.drop_threshold ?? 0.05) * 100;

  return (
    <div className="h-screen w-full flex flex-col bg-[#0A0A0A] text-white overflow-hidden" data-testid="dashboard-root">
      <Toaster theme="dark" position="top-right" />

      {/* TOP BAR */}
      <header className="flex items-center justify-between px-6 py-3 border-b border-white/10 bg-[#121212] shrink-0" data-testid="top-bar">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <div className={`w-2 h-2 rounded-full ${tracking ? "bg-[#32D74B] pulse-dot" : "bg-zinc-600"}`} />
            <span className="font-display uppercase tracking-tight text-xl font-bold">Pinnacle <span className="text-[#007AFF]">Drop</span> Monitor</span>
          </div>
          <span className="text-xs text-zinc-500 uppercase tracking-widest font-mono">Tennis · H2H &amp; Total Games</span>
        </div>

        <div className="flex items-center gap-6">
          <StatusPill label="Last scan" value={status?.last_scan_at ? new Date(status.last_scan_at).toLocaleTimeString() : "—"} icon={<Clock size={14} />} />
          <StatusPill label="Next scan" value={fmtCountdown(nextScanSec)} icon={<Radio size={14} className="text-[#007AFF]" />} mono />
          <StatusPill label="Drop ≥" value={`${threshold.toFixed(1)}%`} icon={<TrendingDown size={14} />} mono />

          {provider === "theoddsapi" && status?.requests_remaining != null && (
            <StatusPill label="Credits" value={status.requests_remaining} icon={<Activity size={14} />} mono />
          )}

          <div className="flex border border-white/15" data-testid="provider-switch">
            {(settings?.providers || ["theoddsapi", "oddspapi"]).map(p => (
              <button
                key={p}
                data-testid={`provider-${p}`}
                onClick={() => setProvider(p)}
                className={`px-2.5 py-1.5 text-[10px] uppercase tracking-widest font-bold transition-colors ${
                  provider === p ? "bg-[#007AFF] text-white" : "text-zinc-400 hover:bg-white/5"
                }`}
              >
                {PROVIDER_LABELS[p] || p}
              </button>
            ))}
          </div>

          <button
            data-testid="tracking-toggle"
            onClick={toggleTracking}
            className={`flex items-center gap-2 px-3 py-1.5 border text-xs font-bold uppercase tracking-widest transition-colors ${
              tracking
                ? "border-[#32D74B]/40 bg-[#32D74B]/10 text-[#32D74B] hover:bg-[#32D74B]/20"
                : "border-white/20 bg-white/5 text-zinc-400 hover:bg-white/10"
            }`}
          >
            <Power size={14} /> {tracking ? "Tracking ON" : "Tracking OFF"}
          </button>

          <button
            data-testid="basketball-toggle"
            onClick={toggleBasketball}
            title="Traccia anche il basket (NBA/WNBA/EuroBasket, via OddsPapi)"
            className={`px-2.5 py-1.5 border text-xs font-bold uppercase tracking-widest transition-colors ${
              basketball
                ? "border-[#FF9F0A]/40 bg-[#FF9F0A]/10 text-[#FF9F0A] hover:bg-[#FF9F0A]/20"
                : "border-white/20 bg-white/5 text-zinc-400 hover:bg-white/10"
            }`}
          >
            🏀 {basketball ? "ON" : "OFF"}
          </button>

          {status?.use_mock_data && (
            <button
              data-testid="mock-toggle-off"
              onClick={async () => { await axios.post(`${API}/mock/false`); await loadAll(); toast.info("Live API mode"); }}
              className="text-[10px] uppercase tracking-widest text-[#FF9F0A] border border-[#FF9F0A]/40 bg-[#FF9F0A]/10 px-2 py-1 hover:bg-[#FF9F0A]/20"
            >
              DEMO MODE · click to disable
            </button>
          )}

          <Button
            data-testid="refresh-btn"
            onClick={manualRefresh}
            disabled={refreshing}
            className="rounded-none bg-[#007AFF] hover:bg-[#0056b3] text-white uppercase tracking-wider text-xs font-bold px-4"
          >
            <RefreshCw size={14} className={`mr-2 ${refreshing ? "animate-spin" : ""}`} />
            {refreshing ? "Scanning" : "Refresh"}
          </Button>

          <SettingsDialog settings={settings} onSaved={loadAll} />
        </div>
      </header>

      {/* MAIN */}
      <div className="flex flex-1 overflow-hidden">
        {/* SIDEBAR */}
        <aside className="w-80 border-r border-white/10 bg-[#0A0A0A] p-6 flex flex-col gap-8 overflow-y-auto shrink-0" data-testid="sidebar">
          <SectionTitle icon={<Activity size={12} />}>Session</SectionTitle>
          <div className="grid grid-cols-2 gap-1 -mt-6">
            <StatBox label="Matches" value={totalFixtures} testId="stat-matches" />
            <StatBox label="Drops now" value={activeDrops} accent testId="stat-drops" />
            <StatBox label="Alerts sent" value={status?.last_scan_stats?.alerts_sent ?? 0} testId="stat-alerts" />
            <StatBox label="Tracked" value={status?.last_scan_stats?.selections_tracked ?? 0} testId="stat-tracked" />
          </div>

          <div>
            <SectionTitle icon={<Filter size={12} />}>Filters</SectionTitle>
            <div className="space-y-6">
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
          </div>

          <div>
            <SectionTitle icon={<TrendingDown size={12} />}>Source</SectionTitle>
            <BookRow label="Pinnacle" role="Sharp" />
            <div className="mt-2 text-[10px] text-zinc-600 leading-relaxed">
              Traccia i cali di quota H2H e Total Games su Pinnacle. Quando cala, il denaro sharp si è mosso — i soft book seguono in ritardo.
            </div>
          </div>

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
        <main className="flex-1 flex flex-col overflow-hidden">
          <section className="flex-1 overflow-auto border-b border-white/10" data-testid="lines-table-section">
            <table className="w-full text-sm text-left font-mono whitespace-nowrap">
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
                          ? <>Monitoro i match tennis con inizio nei prossimi 60 minuti. Prossimo controllo tra <span className="text-white">{fmtCountdown(nextScanSec)}</span>.</>
                          : "Attiva il tracciamento dal pulsante in alto per iniziare a monitorare le quote."}
                      </div>
                    </td>
                  </tr>
                )}
                {rows.map((r, i) => (
                  <tr key={i} className={`border-b border-white/5 hover:bg-white/5 transition-colors ${r.is_drop ? "bg-[#32D74B]/5" : ""}`} data-testid={`line-row-${i}`}>
                    <Td className="text-zinc-400">
                      <div className="flex flex-col leading-tight">
                        <span>{fmtTime(r.match.start_time)}</span>
                        <span className="text-[10px] text-zinc-600">in {minsUntil(r.match.start_time)}m</span>
                      </div>
                    </Td>
                    <Td>
                      <div className="flex flex-col leading-tight">
                        <span className="text-white">{r.match.player1} <span className="text-zinc-600">vs</span> {r.match.player2}</span>
                        <span className="text-[10px] text-zinc-500 uppercase tracking-widest">{r.match.sport_emoji} {r.match.tournament}</span>
                      </div>
                    </Td>
                    <Td className="text-zinc-400 text-xs uppercase tracking-widest">{r.market_name}</Td>
                    <Td className="text-white">{r.label}</Td>
                    <Td className="text-right text-zinc-500">{r.open_price?.toFixed(2)}</Td>
                    <Td className="text-right text-white font-semibold">{r.price?.toFixed(2)}</Td>
                    <Td className="text-right"><DropBadge drop={r.drop_from_open} isDrop={r.is_drop} /></Td>
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
              <span className="text-zinc-600">{alerts.length} events</span>
            </div>
            {alerts.length === 0 && (
              <div className="text-zinc-600 py-6 text-center">Nessun alert ancora. I cali di quota Pinnacle verranno stampati qui.</div>
            )}
            {alerts.map((a) => (
              <div key={a.created_at + a.label} className="py-1 border-b border-white/5 flex items-start gap-3" data-testid="alert-row">
                <span className="text-zinc-600">{new Date(a.created_at).toLocaleTimeString()}</span>
                {a.telegram_ok ? <CheckCircle2 size={12} className="text-[#32D74B] shrink-0 mt-0.5" /> : <XCircle size={12} className="text-[#FF3B30] shrink-0 mt-0.5" />}
                <span className="text-zinc-300">
                  {SPORT_EMOJI[a.sport] ? `${SPORT_EMOJI[a.sport]} ` : ""}
                  <span className="text-white">{a.player1} vs {a.player2}</span>
                  <span className="text-zinc-600"> · {a.market_name} — </span>
                  <span className="text-white">{a.label}</span>
                  <span className="text-zinc-600"> · </span>
                  {a.prev_price?.toFixed(2)} → <span className="text-white">{a.price?.toFixed(2)}</span>
                  <span className="ml-2 text-[#32D74B]">-{(a.drop_last * 100).toFixed(1)}%</span>
                </span>
              </div>
            ))}
          </section>
        </main>
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
    <div className="border border-white/10 p-3 bg-[#0A0A0A]" data-testid={testId}>
      <div className="text-[10px] uppercase tracking-widest text-zinc-500">{label}</div>
      <div className={`font-display text-3xl font-bold tracking-tight ${accent ? "text-[#32D74B]" : "text-white"}`}>{value}</div>
    </div>
  );
}
function BookRow({ label, role }) {
  return (
    <div className="flex items-center justify-between border border-white/10 px-3 py-2">
      <span className="text-white">{label}</span>
      <span className="text-[10px] uppercase tracking-widest text-[#007AFF]">{role}</span>
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

function SettingsDialog({ settings, onSaved }) {
  const [open, setOpen] = useState(false);
  const [drop, setDrop] = useState(5);
  const [token, setToken] = useState("");
  const [chatId, setChatId] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (settings && open) {
      setDrop(Math.round((settings.drop_threshold ?? 0.05) * 1000) / 10);
    }
  }, [settings, open]);

  const save = async () => {
    setSaving(true);
    try {
      const body = { drop_threshold: drop / 100 };
      if (token) body.telegram_token = token;
      if (chatId) body.telegram_chat_id = chatId;
      await axios.put(`${API}/settings`, body);
      toast.success("Impostazioni salvate");
      setToken(""); setChatId("");
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
      <DialogContent className="bg-[#0A0A0A] border-white/10 rounded-none max-w-lg" data-testid="settings-dialog">
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
            <div className="text-[10px] text-zinc-600 mt-2">Alert quando una quota Pinnacle cala di almeno questa % tra due scansioni.</div>
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

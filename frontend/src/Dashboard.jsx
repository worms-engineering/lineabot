import { useCallback, useEffect, useMemo, useState } from "react";
import axios from "axios";
import { Toaster, toast } from "sonner";
import {
  RefreshCw, Settings, Send, Zap, Clock, Radio, TrendingUp,
  AlertCircle, CheckCircle2, XCircle, Activity, Filter
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

const BOOK_LABELS = {
  bet365: "Bet365",
  betfair: "Betfair",
  snai: "Snai",
  pinnacle: "Pinnacle",
};
const ALL_SOFT_BOOKS = ["bet365", "betfair", "snai"];

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
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
  const [minEdge, setMinEdge] = useState(0);
  const [onlyValue, setOnlyValue] = useState(true);
  const [now, setNow] = useState(Date.now());

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
    setRefreshing(true);
    try {
      const r = await axios.post(`${API}/refresh`);
      toast.success(`Scan completo — ${r.data.stats.value_bets_found} value bet, ${r.data.stats.alerts_sent} alert Telegram`);
      await loadAll();
    } catch (e) {
      toast.error("Errore durante lo scan: " + (e?.response?.data?.detail || e.message));
    } finally {
      setRefreshing(false);
    }
  };

  const nextScanSec = useMemo(() => {
    if (!status?.next_scan_at) return null;
    const diff = new Date(status.next_scan_at).getTime() - now;
    return Math.max(0, Math.floor(diff / 1000));
  }, [status, now]);

  const rows = useMemo(() => {
    if (!snapshot?.matches) return [];
    const out = [];
    for (const m of snapshot.matches) {
      for (const vb of m.value_bets || []) {
        if (onlyValue && !vb.is_value) continue;
        if (vb.edge * 100 < minEdge) continue;
        out.push({ ...vb, match: m });
      }
    }
    out.sort((a, b) => b.edge - a.edge);
    return out;
  }, [snapshot, onlyValue, minEdge]);

  const totalMatches = snapshot?.matches?.length || 0;
  const totalValueBets = rows.filter(r => r.is_value).length;
  const threshold = (settings?.edge_threshold ?? 0.03) * 100;

  return (
    <div className="h-screen w-full flex flex-col bg-[#0A0A0A] text-white overflow-hidden" data-testid="dashboard-root">
      <Toaster theme="dark" position="top-right" />

      {/* TOP BAR */}
      <header className="flex items-center justify-between px-6 py-3 border-b border-white/10 bg-[#121212] shrink-0" data-testid="top-bar">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <div className="w-2 h-2 rounded-full bg-[#32D74B] pulse-dot" />
            <span className="font-display uppercase tracking-tight text-xl font-bold">Tennis <span className="text-[#007AFF]">Value</span> Monitor</span>
          </div>
          <span className="text-xs text-zinc-500 uppercase tracking-widest font-mono">Sharp × Soft · Over/Under Games</span>
        </div>

        <div className="flex items-center gap-6">
          <StatusPill label="Last scan" value={status?.last_scan_at ? new Date(status.last_scan_at).toLocaleTimeString() : "—"} icon={<Clock size={14} />} />
          <StatusPill label="Next scan" value={fmtCountdown(nextScanSec)} icon={<Radio size={14} className="text-[#007AFF]" />} mono />
          <StatusPill label="Threshold" value={`+${threshold.toFixed(1)}%`} icon={<TrendingUp size={14} />} mono />

          {status?.use_mock_data && (
            <button
              data-testid="mock-toggle-off"
              onClick={async () => { await axios.post(`${API}/mock/false`); await loadAll(); toast.info("Live API mode"); }}
              className="text-[10px] uppercase tracking-widest text-[#FF9F0A] border border-[#FF9F0A]/40 bg-[#FF9F0A]/10 px-2 py-1 hover:bg-[#FF9F0A]/20"
            >
              DEMO MODE · click to disable
            </button>
          )}
          {!status?.use_mock_data && status?.last_scan_error && (
            <button
              data-testid="mock-toggle-on"
              onClick={async () => { await axios.post(`${API}/mock/true`); await axios.post(`${API}/refresh`); await loadAll(); toast.success("Demo data enabled"); }}
              className="text-[10px] uppercase tracking-widest text-zinc-400 border border-white/20 px-2 py-1 hover:bg-white/5"
            >
              Enable demo data
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
            <StatBox label="Matches" value={totalMatches} testId="stat-matches" />
            <StatBox label="Value Bets" value={totalValueBets} accent testId="stat-valuebets" />
            <StatBox label="Alerts Sent" value={status?.last_scan_stats?.alerts_sent ?? 0} testId="stat-alerts" />
            <StatBox label="Refresh/h" value={`${settings ? 60 / settings.refresh_minutes : 6}`} testId="stat-refresh" />
          </div>

          <div>
            <SectionTitle icon={<Filter size={12} />}>Filters</SectionTitle>
            <div className="space-y-6">
              <div className="flex items-center justify-between">
                <Label className="text-xs uppercase tracking-widest text-zinc-400">Only value bets</Label>
                <Switch data-testid="filter-only-value" checked={onlyValue} onCheckedChange={setOnlyValue} />
              </div>
              <div>
                <div className="flex justify-between text-xs uppercase tracking-widest text-zinc-400 mb-2">
                  <span>Min edge shown</span>
                  <span className="font-mono text-white">{minEdge.toFixed(1)}%</span>
                </div>
                <Slider
                  data-testid="filter-min-edge"
                  min={0} max={20} step={0.5}
                  value={[minEdge]}
                  onValueChange={(v) => setMinEdge(v[0])}
                />
              </div>
            </div>
          </div>

          <div>
            <SectionTitle icon={<Zap size={12} />}>Books tracked</SectionTitle>
            <div className="space-y-2 text-sm font-mono">
              <BookRow label="Pinnacle" role="Sharp" active />
              {(settings?.soft_books || ALL_SOFT_BOOKS).map(b => (
                <BookRow key={b} label={BOOK_LABELS[b] || b} role="Soft" active />
              ))}
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
          <section className="flex-1 overflow-auto border-b border-white/10" data-testid="value-table-section">
            <table className="w-full text-sm text-left font-mono whitespace-nowrap">
              <thead className="sticky top-0 z-10 bg-[#121212] border-b border-white/20 text-zinc-500 text-[10px] uppercase tracking-widest">
                <tr>
                  <Th>Start</Th>
                  <Th>Match</Th>
                  <Th>Market</Th>
                  <Th>Side</Th>
                  <Th className="text-right">Pinnacle</Th>
                  <Th className="text-right">Fair</Th>
                  <Th className="text-right">Soft</Th>
                  <Th>Book</Th>
                  <Th className="text-right">Edge</Th>
                </tr>
              </thead>
              <tbody data-testid="value-table-body">
                {rows.length === 0 && (
                  <tr>
                    <td colSpan={9} className="p-16 text-center text-zinc-500 text-sm">
                      <div className="font-display uppercase tracking-wider text-2xl mb-2 text-zinc-700">No value bets right now</div>
                      <div>Il monitor scansiona i tornei tennis con inizio nei prossimi 60 minuti. Prossimo controllo tra <span className="text-white">{fmtCountdown(nextScanSec)}</span>.</div>
                    </td>
                  </tr>
                )}
                {rows.map((r, i) => (
                  <tr key={i} className="border-b border-white/5 hover:bg-white/5 transition-colors" data-testid={`vb-row-${i}`}>
                    <Td className="text-zinc-400">
                      <div className="flex flex-col leading-tight">
                        <span>{fmtTime(r.match.start_time)}</span>
                        <span className="text-[10px] text-zinc-600">in {minsUntil(r.match.start_time)}m</span>
                      </div>
                    </Td>
                    <Td>
                      <div className="flex flex-col leading-tight">
                        <span className="text-white">{r.match.player1} <span className="text-zinc-600">vs</span> {r.match.player2}</span>
                        <span className="text-[10px] text-zinc-500 uppercase tracking-widest">{r.match.tournament}</span>
                      </div>
                    </Td>
                    <Td className="text-zinc-300">{r.market_name}{r.handicap != null ? ` ${r.handicap}` : ""}</Td>
                    <Td className={r.side === "Over" ? "text-[#FF9F0A]" : "text-[#007AFF]"}>{r.side}</Td>
                    <Td className="text-right text-zinc-300">{r.pinnacle_price?.toFixed(2)}</Td>
                    <Td className="text-right text-zinc-500">{r.fair_price?.toFixed(2)}</Td>
                    <Td className="text-right text-white font-semibold">{r.soft_price?.toFixed(2)}</Td>
                    <Td className="uppercase text-xs tracking-widest text-zinc-300">{BOOK_LABELS[r.soft_book] || r.soft_book}</Td>
                    <Td className="text-right">
                      <EdgeBadge edge={r.edge} isValue={r.is_value} />
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
                <Send size={12} /> Telegram alert log
              </div>
              <span className="text-zinc-600">{alerts.length} events</span>
            </div>
            {alerts.length === 0 && (
              <div className="text-zinc-600 py-6 text-center">Nessun alert ancora inviato. Verranno stampati qui in tempo reale.</div>
            )}
            {alerts.map((a) => (
              <div key={a.dedup_key} className="py-1 border-b border-white/5 flex items-start gap-3" data-testid={`alert-${a.dedup_key}`}>
                <span className="text-zinc-600">{new Date(a.created_at).toLocaleTimeString()}</span>
                {a.telegram_ok ? <CheckCircle2 size={12} className="text-[#32D74B] shrink-0 mt-0.5" /> : <XCircle size={12} className="text-[#FF3B30] shrink-0 mt-0.5" />}
                <span className="text-zinc-300">
                  <span className="text-white">{a.player1} vs {a.player2}</span>
                  <span className="text-zinc-600"> · </span>
                  {a.side}{a.handicap != null ? ` ${a.handicap}` : ""} @ <span className="text-white">{a.soft_price?.toFixed(2)}</span> {BOOK_LABELS[a.soft_book] || a.soft_book}
                  <span className="text-zinc-600"> · Pin </span>{a.pinnacle_price?.toFixed(2)}
                  <span className="ml-2 text-[#32D74B]">+{(a.edge * 100).toFixed(2)}%</span>
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
function BookRow({ label, role, active }) {
  return (
    <div className="flex items-center justify-between border border-white/10 px-3 py-2">
      <span className={role === "Sharp" ? "text-white" : "text-zinc-300"}>{label}</span>
      <span className={`text-[10px] uppercase tracking-widest ${role === "Sharp" ? "text-[#007AFF]" : "text-zinc-500"}`}>{role}</span>
    </div>
  );
}
function Th({ children, className = "" }) {
  return <th className={`px-4 py-2.5 font-semibold ${className}`}>{children}</th>;
}
function Td({ children, className = "" }) {
  return <td className={`px-4 py-2.5 ${className}`}>{children}</td>;
}
function EdgeBadge({ edge, isValue }) {
  const pct = (edge * 100).toFixed(2);
  const positive = edge >= 0;
  const cls = isValue
    ? "bg-[#32D74B]/10 text-[#32D74B] border-[#32D74B]/30"
    : positive
      ? "bg-white/5 text-zinc-300 border-white/10"
      : "bg-[#FF3B30]/10 text-[#FF3B30] border-[#FF3B30]/20";
  return (
    <span className={`inline-block border px-1.5 py-0.5 text-xs font-mono ${cls}`} data-testid="edge-badge">
      {positive ? "+" : ""}{pct}%
    </span>
  );
}

function SettingsDialog({ settings, onSaved }) {
  const [open, setOpen] = useState(false);
  const [edge, setEdge] = useState(3);
  const [books, setBooks] = useState(ALL_SOFT_BOOKS);
  const [token, setToken] = useState("");
  const [chatId, setChatId] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (settings && open) {
      setEdge(Math.round(settings.edge_threshold * 1000) / 10);
      setBooks(settings.soft_books || ALL_SOFT_BOOKS);
    }
  }, [settings, open]);

  const toggleBook = (b) => {
    setBooks(prev => prev.includes(b) ? prev.filter(x => x !== b) : [...prev, b]);
  };

  const save = async () => {
    setSaving(true);
    try {
      const body = {
        edge_threshold: edge / 100,
        soft_books: books,
      };
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
              <span>Edge threshold</span>
              <span className="font-mono text-white">+{edge.toFixed(1)}%</span>
            </div>
            <Slider data-testid="settings-edge-slider" min={0.5} max={15} step={0.1} value={[edge]} onValueChange={(v) => setEdge(v[0])} />
          </div>

          <div>
            <div className="text-xs uppercase tracking-widest text-zinc-400 mb-2">Soft bookmakers</div>
            <div className="space-y-2">
              {ALL_SOFT_BOOKS.map(b => (
                <div key={b} className="flex items-center justify-between border border-white/10 px-3 py-2">
                  <span className="text-sm">{BOOK_LABELS[b]}</span>
                  <Switch data-testid={`settings-book-${b}`} checked={books.includes(b)} onCheckedChange={() => toggleBook(b)} />
                </div>
              ))}
            </div>
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

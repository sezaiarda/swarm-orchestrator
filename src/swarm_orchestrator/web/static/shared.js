// Shared by the overview and every swarm's page: put in each at load, where the
// page says so. Small helpers, the clock, and the swarm buttons.
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
// The server's clock minus this device's: every "ago" is counted on the server's.
let SKEW = 0;
const now = () => Date.now() / 1000 + SKEW;
function dur(s) {
  if (s == null || !isFinite(s)) return "—";
  s = Math.max(0, s);
  if (s < 60) return Math.round(s) + "s";
  if (s < 3600) return Math.round(s / 60) + "m";
  if (s < 86400) { const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60); return h + "h" + (m ? " " + m + "m" : ""); }
  const d = Math.floor(s / 86400), h = Math.round((s % 86400) / 3600); return d + "d" + (h ? " " + h + "h" : "");
}
const rel = (t) => t == null ? "—" : t >= now() ? "in " + dur(t - now()) : dur(now() - t) + " ago";
function hhmm(t) { return new Date(t * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", hourCycle: "h23"}); }
function when(t, withDay) {
  if (!t) return "—";
  const d = new Date(t * 1000), n = new Date(now() * 1000);
  const hm = hhmm(t);
  if (!withDay && d.toDateString() === n.toDateString()) return hm;
  if (Math.abs(d - n) < 6 * 864e5 && !withDay) return d.toLocaleDateString([], {weekday: "short"}) + " " + hm;
  return d.toLocaleDateString([], {weekday: "short", month: "short", day: "numeric"}) + ", " + hm;
}
function day(t) { return t ? new Date(t * 1000).toLocaleDateString([], {weekday: "short", month: "short", day: "numeric"}) : "—"; }
const icon = (p, cls = "") => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" class="${cls}" aria-hidden="true">${p}</svg>`;
// A token's colour as the page has it now: for a canvas, which cannot read var().
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
// One fetch with the ETag the page last saw for `key`: null means "nothing moved".
const ETAGS = {};
async function get(url, key) {
  const r = await fetch(url, {headers: ETAGS[key] ? {"If-None-Match": ETAGS[key]} : {}, cache: "no-store"});
  if (r.status === 304) return null;
  if (!r.ok) throw new Error(r.status);
  ETAGS[key] = r.headers.get("ETag");
  return r.json();
}
// The swarms as buttons. `here` is the slug of the page it is drawn on, if any.
function swarmButtons(swarms, here) {
  return swarms.map(s => `<a class="sw st-${esc(s.status)}" href="/s/${encodeURIComponent(s.slug)}/"${s.slug === here ? ' aria-current="page"' : ""} title="${esc(s.name)} is ${esc(s.word)}${s.needs_you ? `, ${s.needs_you} waiting for you` : ""}"><span class="dot"></span>${esc(s.name)}${s.needs_you ? `<span class="n">${s.needs_you}</span>` : ""}</a>`).join("");
}

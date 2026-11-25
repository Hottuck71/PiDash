/* PiDash kiosk dashboard */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const PAGE_NAMES = { home: "Home", week: "Week", month: "Month", agenda: "Agenda", weather: "Weather" };
const LAYOUT_NAMES = { classic: "Classic", mirrored: "Mirrored", stacked: "Stacked", focus: "Focus", bigclock: "Big clock" };
const THEMES = { dark: "Dark", light: "Light", black: "OLED black", warm: "Warm" };
const DAY = 86400000;

const WX = {
  0: ["Clear", "☀️", "🌙"], 1: ["Mostly clear", "🌤️", "🌙"], 2: ["Partly cloudy", "⛅", "☁️"], 3: ["Overcast", "☁️", "☁️"],
  45: ["Fog", "🌫️"], 48: ["Freezing fog", "🌫️"], 51: ["Light drizzle", "🌦️"], 53: ["Drizzle", "🌦️"], 55: ["Heavy drizzle", "🌧️"],
  56: ["Freezing drizzle", "🌧️"], 57: ["Freezing drizzle", "🌧️"], 61: ["Light rain", "🌦️"], 63: ["Rain", "🌧️"], 65: ["Heavy rain", "🌧️"],
  66: ["Freezing rain", "🌧️"], 67: ["Freezing rain", "🌧️"], 71: ["Light snow", "🌨️"], 73: ["Snow", "🌨️"], 75: ["Heavy snow", "❄️"],
  77: ["Snow grains", "🌨️"], 80: ["Showers", "🌦️"], 81: ["Showers", "🌧️"], 82: ["Heavy showers", "⛈️"], 85: ["Snow showers", "🌨️"],
  86: ["Snow showers", "❄️"], 95: ["Thunderstorm", "⛈️"], 96: ["Thunderstorm + hail", "⛈️"], 99: ["Thunderstorm + hail", "⛈️"],
};
const wxDesc = c => (WX[c] || ["—"])[0];
const wxIcon = (c, day = 1) => { const w = WX[c]; if (!w) return "❔"; return (!day && w[2]) ? w[2] : w[1]; };

const params = new URLSearchParams(location.search);
if (params.has("kiosk")) document.body.classList.add("kiosk");

let state = null, cal = { events: [], errors: [], feeds: 0 }, wx = null;
let page = "home", lastCmd = null, pausedUntil = 0, cycleStart = Date.now(), dimOverride = 0;

/* ---------------- API ---------------- */
async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return r.json();
}
const S = () => state.settings;
// rebuild a part of the screen only when its contents really changed
function setHTML(el, html) {
  if (el._html === html) return false;
  el._html = html; el.innerHTML = html;
  return true;
}
function saveSettings(patch) {
  Object.assign(state.settings, patch);
  if (patch.page) page = patch.page;
  applySettings(); render();
  patch = { ...patch };
  delete patch.page; // the page being looked at is not worth a write to the SD card
  if (!Object.keys(patch).length) return;
  api("/api/settings", patch).then(s => { state.version = s.version; }).catch(() => {});
}
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 1800);
}

/* ---------------- time helpers ---------------- */
const pd = n => String(n).padStart(2, "0");
const ymd = d => `${d.getFullYear()}-${pd(d.getMonth() + 1)}-${pd(d.getDate())}`;
const startOfDay = d => new Date(d.getFullYear(), d.getMonth(), d.getDate());
const DF = {}; // making a date formatter is slow on the Pi, so keep them
const dateText = (d, o) => (DF[JSON.stringify(o)] ||= new Intl.DateTimeFormat(undefined, o)).format(d);
const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
function fmtTime(d) {
  if (S().clock24) return `${pd(d.getHours())}:${pd(d.getMinutes())}`;
  let h = d.getHours() % 12 || 12;
  return `${h}:${pd(d.getMinutes())}${d.getHours() < 12 ? "a" : "p"}`;
}
function relDay(d) {
  const diff = Math.round((startOfDay(d) - startOfDay(new Date())) / DAY);
  if (diff === 0) return "Today"; if (diff === 1) return "Tomorrow"; if (diff === -1) return "Yesterday";
  if (diff > 1 && diff < 7) return `in ${diff} days`;
  return "";
}

/* events */
function evs() { // convert the dates once per calendar download
  if (evs.from !== cal.events) { evs.from = cal.events; evs.list = cal.events.map(e => ({ ...e, s: new Date(e.start), e: new Date(e.end) })); }
  return evs.list;
}
function eventsOn(day, list = evs()) {
  const a = startOfDay(day), b = addDays(a, 1);
  return list.filter(e => e.allDay ? (e.s < b && e.e > a) : (e.s < b && (e.e > a || (+e.e === +e.s && e.s >= a))));
}
function evTime(e) { return e.allDay ? "All day" : fmtTime(e.s); }

/* ---------------- layout / rotation ---------------- */
function applySettings() {
  const s = S(), root = $("#root");
  root.className = "theme-" + (THEMES[s.theme] ? s.theme : "dark");
  updatePhotoMode();
  document.documentElement.style.setProperty("--accent", s.accent || "#4fc3f7");
  root.style.setProperty("--accent", s.accent || "#4fc3f7");
  sizeRoot();
}
function sizeRoot() {
  if (!state) return;
  const root = $("#root"), rot = +S().rotation || 0, W = innerWidth, H = innerHeight;
  const side = rot === 90 || rot === 270;
  const w = side ? H : W, h = side ? W : H;
  root.style.width = w + "px"; root.style.height = h + "px";
  root.style.transform = { 0: "none", 90: `translate(${W}px,0) rotate(90deg)`, 180: `translate(${W}px,${H}px) rotate(180deg)`, 270: `translate(0,${H}px) rotate(270deg)` }[rot];
  const base = Math.min(w, h) / 34 * (+S().fontScale || 1);
  document.documentElement.style.fontSize = Math.max(9, base) + "px";
  root.dataset.portrait = h > w ? "1" : "";
}
addEventListener("resize", () => { sizeRoot(); render(); });

/* ---------------- widgets ---------------- */
const W = {};
W.clock = () => {
  const n = new Date();
  let h = n.getHours(), ampm = "";
  if (!S().clock24) { ampm = h < 12 ? "AM" : "PM"; h = h % 12 || 12; }
  const sec = S().showSeconds ? `<span class="sec">:${pd(n.getSeconds())}</span>` : "";
  const next = evs().find(e => !e.allDay && e.s > n && e.s - n < DAY);
  return `<div class="panel"><div class="clock-time">${S().clock24 ? pd(h) : h}:${pd(n.getMinutes())}${sec}${ampm ? `<span class="ampm">${ampm}</span>` : ""}</div>
    <div class="clock-date">${dateText(n, { weekday: "long", month: "long", day: "numeric" })}</div>
    ${next ? `<div class="clock-next">Next: <b>${esc(next.title)}</b> <span class="muted">${fmtTime(next.s)}</span></div>` : ""}</div>`;
};
W.weather = (opts = {}) => {
  if (!wx) return `<div class="panel"><h2>Weather</h2><div class="muted">Loading…</div></div>`;
  if (wx.error) return `<div class="panel"><h2>Weather</h2><div class="muted">${wx.error === "no-location" ? "Set WEATHER_LOCATION in dashboard-settings.txt" : "Weather unavailable: " + esc(wx.error)}</div></div>`;
  const c = wx.current, d = wx.daily, u = wx.current_units || {};
  const n = opts.days || 5;
  let days = "";
  for (let i = 0; i < Math.min(n, d.time.length); i++) {
    const dt = new Date(d.time[i] + "T12:00");
    days += `<div class="d">${i === 0 ? "Today" : dateText(dt, { weekday: "short" })}<span class="i">${wxIcon(d.weather_code[i])}</span>
      <span class="hi">${Math.round(d.temperature_2m_max[i])}°</span> <span class="lo">${Math.round(d.temperature_2m_min[i])}°</span>
      ${d.precipitation_probability_max[i] > 15 ? `<div class="pp">💧${d.precipitation_probability_max[i]}%</div>` : ""}</div>`;
  }
  return `<div class="panel"><h2>${esc(wx.locationName || "Weather")}<span class="sub">feels ${Math.round(c.apparent_temperature)}°</span></h2>
    <div class="wx-now"><div class="wx-icon">${wxIcon(c.weather_code, c.is_day)}</div><div><div class="wx-temp">${Math.round(c.temperature_2m)}°</div></div>
    <div><div class="wx-desc">${wxDesc(c.weather_code)}</div><div class="wx-meta">H ${Math.round(d.temperature_2m_max[0])}° · L ${Math.round(d.temperature_2m_min[0])}°<br>💨 ${Math.round(c.wind_speed_10m)} ${esc(u.wind_speed_10m || "")} · 💧 ${c.relative_humidity_2m}%</div></div></div>
    ${opts.noDays ? "" : `<div class="wx-days" style="--n:${n}">${days}</div>`}</div>`;
};
W.forecast = () => W.weather({ days: 7 });
W.agenda = (opts = {}) => opts.grid && cal.feeds ? calGridHTML(7) : `<div class="panel"><h2>Coming up<span class="sub">${cal.feeds ? "" : "add COZI_URL in dashboard-settings.txt"}</span></h2>${agendaHTML(S().agendaDays || 7, false)}</div>`;
W.countdowns = () => {
  const list = countdownList();
  return `<div class="panel"><h2>Countdowns</h2><div class="cd-list">${list.length ? list.slice(0, 5).map(c =>
    `<div class="cd"><span class="e">${esc(c.emoji || "📅")}</span><span class="num">${c.days}</span><span class="lbl">${esc(c.title)}<small>${c.days === 0 ? "today!" : c.days === 1 ? "day to go" : "days to go"}</small></span></div>`).join("")
    : `<div class="muted">Add COUNTDOWN_1 in dashboard-settings.txt.</div>`}</div></div>`;
};
function countdownList() {
  const today = startOfDay(new Date());
  return (state.countdowns || []).map(c => ({ ...c, days: Math.round((new Date(c.date + "T00:00") - today) / DAY) }))
    .filter(c => c.days >= 0).sort((a, b) => a.days - b.days);
}

// the big calendar on the overview: today and the next days side by side
function calGridHTML(days) {
  const list = evs(), now = new Date(), today = startOfDay(now);
  let html = "";
  for (let i = 0; i < days; i++) {
    const day = addDays(today, i);
    const items = eventsOn(day, list).filter(e => i > 0 || e.allDay || e.e > now || +e.e === +e.s);
    let w = "";
    if (wx && wx.daily) { const k = wx.daily.time.indexOf(ymd(day)); if (k >= 0) { const pp = wx.daily.precipitation_probability_max[k]; w = `<span class="i">${wxIcon(wx.daily.weather_code[k])}</span><b>${Math.round(wx.daily.temperature_2m_max[k])}°</b> ${Math.round(wx.daily.temperature_2m_min[k])}°${pp > 15 ? ` <span class="pp">${pp}%</span>` : ""}`; } }
    html += `<div class="cg-col ${i ? "" : "today"}"><div class="cg-h"><span class="dd">${day.getDate()}</span><span class="dn">${i ? dateText(day, { weekday: "short" }) : "Today"}</span></div><div class="cg-w">${w}</div><div class="cg-list">${
      items.map(e => `<div class="cg-ev ${e.allDay ? "allday" : ""} ${!e.allDay && e.s <= now && e.e > now ? "now" : ""}" style="--c:${esc(e.color)}">${e.allDay ? "" : `<span class="t">${e.s < startOfDay(day) ? "until " + fmtTime(e.e) : fmtTime(e.s)}</span>`}<span class="n">${esc(e.title)}</span></div>`).join("")
      || `<div class="cg-none">Free</div>`}</div></div>`;
  }
  const err = cal.errors.length ? `<div class="cg-err">⚠ ${esc(cal.errors[0].error)}</div>` : "";
  return `<div class="cal-grid" style="--n:${days}">${html}</div>${err}`;
}
// events that do not fit in a day become "+2 more"
function fitCalGrid(root) {
  const work = $$(".cg-list", root).map(el => {
    const kids = [...el.children], room = el.clientHeight;
    return { el, kids, room, over: el.scrollHeight > room + 1, ends: kids.map(k => k.offsetTop + k.offsetHeight), line: parseFloat(getComputedStyle(el).fontSize) * 1.6 };
  });
  for (const { el, kids, room, over, ends, line } of work) {
    if (!over || kids.length < 2) continue;
    let keep = ends.filter(b => b <= room - line).length;
    keep = Math.max(1, Math.min(keep, kids.length - 1));
    kids.slice(keep).forEach(k => k.remove());
    const more = document.createElement("div");
    more.className = "cg-more"; more.textContent = `+${kids.length - keep} more`;
    el.append(more);
  }
}

function agendaHTML(days, cols) {
  const list = evs(), now = new Date(), today = startOfDay(now);
  let html = "", count = 0;
  for (let i = 0; i < days; i++) {
    const day = addDays(today, i);
    const items = eventsOn(day, list).filter(e => i > 0 || e.allDay || e.e > now || +e.e === +e.s && e.s > addDays(now, -1));
    if (!items.length) continue;
    count += items.length;
    html += `<div class="ag-day"><div class="ag-day-h">${dateText(day, { weekday: "short", month: "short", day: "numeric" })}<span class="rel">${relDay(day)}</span></div>
      ${items.map(e => `<div class="ev ${!e.allDay && e.s <= now && e.e > now ? "now" : ""}" style="--c:${esc(e.color)}"><span class="t">${evTime(e)}</span><span class="n">${esc(e.title)}</span>${cal.feeds > 1 ? `<span class="cal">${esc(e.calendar)}</span>` : ""}</div>`).join("")}</div>`;
  }
  if (!count) html = `<div class="big-msg">${cal.feeds ? "Nothing scheduled 🎉" : "No calendar connected yet"}${cal.errors.length ? `<small>⚠ ${esc(cal.errors[0].error)}</small>` : ""}</div>`;
  return `<div class="agenda ${cols ? "cols" : ""}">${html}</div>`;
}

/* ---------------- pages ---------------- */
function renderHome() {
  const s = S(), root = $("#root"), homeGrid = $("#homeGrid"), fits = [];
  const wide = !root.dataset.portrait && (s.layout === "stacked" || s.layout === "focus");
  homeGrid.className = "home-grid layout-" + (LAYOUT_NAMES[s.layout] ? s.layout : "stacked");
  const slots = $$(".slot", homeGrid);
  const setup = !cal.feeds && !(s.location && (s.location.name || s.location.lat));
  slots.forEach((el, i) => {
    let w = (s.slots || [])[i] || "clock";
    // nothing to show: leave the room to the calendar
    el.classList.toggle("blank", w === "countdowns" && !countdownList().length);
    if (setup && i === 2) { setHTML(el, setupHTML()); return; }
    const grid = w === "agenda" && i === 2 && wide;
    // the calendar columns already show each day's weather
    const fresh = setHTML(el, (W[w] || W.clock)({ grid, noDays: wide && w === "weather" && (s.slots || [])[2] === "agenda" && cal.feeds > 0, days: w === "weather" && s.layout === "bigclock" ? 3 : undefined }));
    if (grid) fits.push([el, fresh]);
  });
  for (const [el, fresh] of fits) { // after every box is filled in: fit the events to the room there is
    const room = [el.clientWidth, el.clientHeight, document.documentElement.style.fontSize].join();
    if (!fresh && el._room === room) continue;
    if (!fresh) el.innerHTML = el._html;
    el._room = room;
    fitCalGrid(el);
  }
}
function setupHTML() {
  return `<div class="panel"><div class="big-msg"><div style="font-size:2.5rem">👋</div><div>Welcome to <b>PiDash</b></div>
    <div>Turn the Pi off, put the SD card in your PC and open</div><b>dashboard-settings.txt</b>
    <div>to add your Cozi calendar link and your weather location.</div></div></div>`;
}
function renderWeek() {
  const s = S(), today = startOfDay(new Date());
  let start = addDays(today, -((today.getDay() - (s.weekStart || 0) + 7) % 7));
  const list = evs();
  let html = "";
  for (let i = 0; i < 7; i++) {
    const day = addDays(start, i), items = eventsOn(day, list);
    let wxs = "";
    if (wx && wx.daily) { const k = wx.daily.time.indexOf(ymd(day)); if (k >= 0) wxs = `${wxIcon(wx.daily.weather_code[k])} ${Math.round(wx.daily.temperature_2m_max[k])}°/${Math.round(wx.daily.temperature_2m_min[k])}°`; }
    html += `<div class="wk-col ${+day === +today ? "today" : ""}"><div class="wk-h"><div class="dn">${dateText(day, { weekday: "short" })}</div><div class="dd">${day.getDate()}</div></div>
      <div class="wk-wx">${wxs}</div>${items.map(e => `<div class="wk-ev ${e.allDay ? "allday" : ""}" style="--c:${esc(e.color)}">${e.allDay ? "" : `<span class="t">${fmtTime(e.s)}</span>`}${esc(e.title)}</div>`).join("")}</div>`;
  }
  setHTML($("#weekView"), html);
}
function renderMonth() {
  const s = S(), now = new Date(), today = startOfDay(now), first = new Date(now.getFullYear(), now.getMonth(), 1);
  const start = addDays(first, -((first.getDay() - (s.weekStart || 0) + 7) % 7));
  const list = evs();
  let dows = "", cells = "";
  for (let i = 0; i < 7; i++) dows += `<div>${dateText(addDays(start, i), { weekday: "short" })}</div>`;
  const weeks = Math.ceil(((first - start) / DAY + new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate()) / 7);
  for (let i = 0; i < weeks * 7; i++) {
    const day = addDays(start, i), items = eventsOn(day, list), max = weeks > 5 ? 2 : 3;
    cells += `<div class="mo-cell ${day.getMonth() !== now.getMonth() ? "other" : ""} ${+day === +today ? "today" : ""}"><div class="mo-num">${day.getDate()}</div>
      ${items.slice(0, max).map(e => `<div class="mo-ev ${e.allDay ? "allday" : ""}" style="--c:${esc(e.color)}">${e.allDay ? "" : fmtTime(e.s) + " "}${esc(e.title)}</div>`).join("")}
      ${items.length > max ? `<div class="mo-more">+${items.length - max} more</div>` : ""}</div>`;
  }
  setHTML($("#monthView"), `<div class="mo-title"><span>${dateText(now, { month: "long", year: "numeric" })}</span><span class="muted" style="font-size:.9rem">${cal.events.length ? "" : "No events loaded"}</span></div>
    <div class="mo-dows">${dows}</div><div class="mo-grid" style="grid-template-rows:repeat(${weeks},1fr)">${cells}</div>`);
}
function renderAgenda() {
  setHTML($("#agendaView"), agendaHTML(14, !$("#root").dataset.portrait));
  $("#agendaSub").textContent = "next 14 days";
}
function renderWeatherPage() {
  const el = $("#weatherView");
  if (!wx || wx.error) { setHTML(el, W.weather()); return; }
  const c = wx.current, h = wx.hourly, d = wx.daily, u = wx.current_units || {};
  const nowIdx = Math.max(0, h.time.findIndex(t => new Date(t) >= new Date(Date.now() - 3600000)));
  let hours = "";
  for (let i = nowIdx; i < nowIdx + 12 && i < h.time.length; i++) {
    const t = new Date(h.time[i]);
    hours += `<div class="h">${i === nowIdx ? "Now" : fmtTime(t).replace(":00", "")}<span class="i">${wxIcon(h.weather_code[i], h.is_day[i])}</span><b>${Math.round(h.temperature_2m[i])}°</b>${h.precipitation_probability[i] > 10 ? `<div class="pp" style="color:#64b5f6;font-size:.7rem">💧${h.precipitation_probability[i]}%</div>` : ""}</div>`;
  }
  let week = "";
  for (let i = 0; i < d.time.length; i++) {
    const dt = new Date(d.time[i] + "T12:00");
    week += `<div class="d"><div>${i === 0 ? "Today" : dateText(dt, { weekday: "long" })}</div><div class="i">${wxIcon(d.weather_code[i])}</div>
      <div><span class="hi">${Math.round(d.temperature_2m_max[i])}°</span> <span class="muted">${Math.round(d.temperature_2m_min[i])}°</span></div>
      <div class="muted" style="font-size:.75rem">${wxDesc(d.weather_code[i])}</div>${d.precipitation_probability_max[i] > 10 ? `<div style="color:#64b5f6;font-size:.8rem">💧${d.precipitation_probability_max[i]}%</div>` : ""}</div>`;
  }
  const sr = new Date(d.sunrise[0]), ss = new Date(d.sunset[0]);
  setHTML(el, `<div class="panel"><h2>${esc(wx.locationName || "")}<span class="sub">updated ${fmtTime(new Date(c.time))}</span></h2><div class="wx-hero">
    <div class="wx-icon">${wxIcon(c.weather_code, c.is_day)}</div><div class="wx-temp">${Math.round(c.temperature_2m)}°</div>
    <div><div class="wx-desc">${wxDesc(c.weather_code)}</div><div class="wx-meta">Feels like ${Math.round(c.apparent_temperature)}° · H ${Math.round(d.temperature_2m_max[0])}° L ${Math.round(d.temperature_2m_min[0])}°<br>
    💨 ${Math.round(c.wind_speed_10m)} ${esc(u.wind_speed_10m || "")} · 💧 ${c.relative_humidity_2m}% · UV ${Math.round(d.uv_index_max[0] || 0)}<br>🌅 ${fmtTime(sr)} · 🌇 ${fmtTime(ss)}</div></div></div></div>
    <div class="wx-hours">${hours}</div><div class="wx-week">${week}</div>`);
}
function currentPage() { const s = S(); return (s.pages || []).includes(page) ? page : (s.pages || ["home"])[0] || "home"; }
function render() {
  if (!state) return;
  const cur = currentPage();
  $$(".page").forEach(p => p.classList.toggle("active", p.dataset.page === cur));
  ({ home: renderHome, week: renderWeek, month: renderMonth, agenda: renderAgenda, weather: renderWeatherPage }[cur] || renderHome)();
  const pages = S().pages || [];
  setHTML($("#dots"), pages.map(p => `<span class="${p === cur ? "on" : ""}"></span>`).join(""));
  $("#stLeft").textContent = PAGE_NAMES[cur] + (S().autocycle ? " · auto" : "");
}

/* ---------------- navigation ---------------- */
function go(delta, user = true) {
  const s = S(), pages = s.pages && s.pages.length ? s.pages : ["home"];
  const i = Math.max(0, pages.indexOf(currentPage()));
  const next = pages[(i + delta + pages.length) % pages.length];
  if (user) pausedUntil = Date.now() + 60000;
  cycleStart = Date.now();
  saveSettings({ page: next });
}
function setPage(p) { pausedUntil = Date.now() + 60000; cycleStart = Date.now(); saveSettings({ page: p }); }
function cycleLayout(delta = 1) {
  const ks = Object.keys(LAYOUT_NAMES), i = ks.indexOf(S().layout);
  const l = ks[(i + delta + ks.length) % ks.length];
  saveSettings({ layout: l, page: "home" }); toast("Layout: " + LAYOUT_NAMES[l]);
}
function swapSlots(back) {
  const sl = [...(S().slots || [])];
  if (back) sl.push(sl.shift()); else sl.unshift(sl.pop());
  saveSettings({ slots: sl, page: "home" }); toast("Widgets moved");
}
function cycleTheme() { const ks = Object.keys(THEMES), i = ks.indexOf(S().theme); const t = ks[(i + 1) % ks.length]; saveSettings({ theme: t }); toast("Theme: " + THEMES[t]); }

function doAction(a, v) {
  switch (a) {
    case "next": go(1); break;
    case "prev": go(-1); break;
    case "layout": cycleLayout(1); break;
    case "swap": swapSlots(false); break;
    case "swapback": swapSlots(true); break;
    case "theme": cycleTheme(); break;
    case "page": if (v) setPage(v); break;
    case "refresh": refreshNow(); break;
    case "reload": location.reload(); break;
    case "clock24": saveSettings({ clock24: !S().clock24 }); break;
    case "wake": dimOverride = Date.now() + 10 * 60000; updateDim(); break;
  }
}

/* ---------------- menu ---------------- */
function openMenu() {
  const s = S();
  const chips = (obj, cur, act) => Object.entries(obj).map(([k, n]) => `<button class="chip ${String(cur) === String(k) ? "on" : ""}" data-${act}="${k}">${n}</button>`).join("");
  $("#mPages").innerHTML = chips(Object.fromEntries((s.pages || []).map(p => [p, PAGE_NAMES[p]])), currentPage(), "page");
  $("#mLayouts").innerHTML = chips(LAYOUT_NAMES, s.layout, "layout");
  $("#mThemes").innerHTML = chips(THEMES, s.theme, "theme");
  $("#mRot").innerHTML = chips({ 0: "0°", 90: "90°", 180: "180°", 270: "270°" }, s.rotation, "rot");
  $("#mCycle").innerHTML = chips({ 0: "Off", 15: "15 s", 30: "30 s", 60: "1 min", 300: "5 min" }, s.autocycle, "cycle");
  $("#mFont").innerHTML = chips({ 0.85: "Small", 1: "Normal", 1.15: "Large", 1.3: "Huge" }, s.fontScale, "font");
  $("#mPhotos").innerHTML = chips({ 0: "Off", 15: "15 s", 30: "30 s", 60: "1 min", 300: "5 min" }, s.photoSeconds, "photos");
  $("#mInfo").textContent = `Photos: ${photoTotal}${photoSkipped ? ` (${photoSkipped} files can't be shown - use JPG or PNG)` : ""}  ·  Videos: ${vids.ready.length}${vids.pending ? ` (${vids.pending} being prepared)` : ""}${vids.failed ? ` (${vids.failed} can't be played)` : ""}  ·  Keys: ←→ pages, ↑↓ layout, S move widgets, T theme, P next photo, R check calendar, M menu`;
  $("#menu").classList.add("open");
}
function closeOverlays() { $$(".overlay").forEach(o => o.classList.remove("open")); }
$("#menuBtn").addEventListener("click", e => { e.stopPropagation(); openMenu(); });
$("#menu").addEventListener("click", e => {
  const b = e.target.closest("button,[data-close]");
  if (e.target === $("#menu") || (b && b.hasAttribute("data-close"))) return closeOverlays();
  if (!b) return;
  const d = b.dataset;
  if (d.page) setPage(d.page);
  else if (d.layout) saveSettings({ layout: d.layout, page: "home" });
  else if (d.theme) saveSettings({ theme: d.theme });
  else if (d.rot !== undefined) saveSettings({ rotation: +d.rot });
  else if (d.cycle !== undefined) saveSettings({ autocycle: +d.cycle });
  else if (d.photos !== undefined) { saveSettings({ photoSeconds: +d.photos }); if (+d.photos && !$("#bg > *")) nextPhoto(); }
  else if (d.font !== undefined) saveSettings({ fontScale: +d.font });
  else if (d.act) doAction(d.act);
  if ($("#menu").classList.contains("open")) openMenu();
});

/* ---------------- touch / click / keys ---------------- */
$("#pages").addEventListener("click", e => {
  const act = e.target.closest("[data-act]"); if (act) doAction(act.dataset.act);
});
$("#stRight").addEventListener("click", () => refreshNow());
let tStart = null;
$("#root").addEventListener("pointerdown", e => {
  tStart = { x: e.clientX, y: e.clientY, t: Date.now() };
  if ($("#dim").classList.contains("on")) { dimOverride = Date.now() + 2 * 60000; updateDim(); }
});
$("#root").addEventListener("pointerup", e => {
  if (!tStart || $$(".overlay.open").length) return;
  // translate swipe into the rotated frame
  let dx = e.clientX - tStart.x, dy = e.clientY - tStart.y;
  const rot = +S().rotation || 0;
  if (rot === 90) [dx, dy] = [dy, -dx]; else if (rot === 180) [dx, dy] = [-dx, -dy]; else if (rot === 270) [dx, dy] = [-dy, dx];
  if (Date.now() - tStart.t < 800 && Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) go(dx < 0 ? 1 : -1);
  else if (Date.now() - tStart.t < 800 && Math.abs(dy) > 80 && Math.abs(dy) > Math.abs(dx) * 1.5 && currentPage() === "home") cycleLayout(dy < 0 ? 1 : -1);
  tStart = null;
});
addEventListener("keydown", e => {
  if ($("#menu").classList.contains("open") && (e.key === "Escape" || e.key.toLowerCase() === "m")) return closeOverlays();
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  const map = { ArrowRight: "next", ArrowLeft: "prev", PageDown: "next", PageUp: "prev", " ": "next", ArrowUp: "layout", s: "swap", t: "theme", r: "refresh", c: "clock24" };
  if (map[k]) { e.preventDefault(); doAction(map[k]); }
  else if (k === "ArrowDown") { e.preventDefault(); cycleLayout(-1); }
  else if (k === "m" || k === "Enter") openMenu();
  else if (k === "p") nextPhoto();
  else if (k === "a") { saveSettings({ autocycle: S().autocycle ? 0 : 30 }); toast(S().autocycle ? "Auto-cycle on" : "Auto-cycle off"); }
  else if (k === "o") saveSettings({ rotation: ((+S().rotation || 0) + 90) % 360 });
  else if (/^[1-9]$/.test(k) && S().pages[+k - 1]) setPage(S().pages[+k - 1]);
});

/* ---------------- photo and video background ---------------- */
// photos holds what is in the slideshow: "photo/<id>" and "video/<id>"
let photos = [], photoQueue = [], photoKey = "", photoTotal = 0, photoSkipped = 0, photoShown = null, photoAt = 0, photoBusy = false;
let vids = { ready: [], pending: 0, failed: 0 };
const badVideos = new Set(); // would not play here
const photosOn = () => photos.length > 0 && +S().photoSeconds > 0;
const bgVideo = () => $("#bg video.on:last-child");
function dropMedia(el) {
  if (el.tagName === "VIDEO") { el.onended = null; el.pause(); el.removeAttribute("src"); el.load(); } // gives the memory back
  el.remove();
}
function updatePhotoMode() {
  const on = photosOn() && !!$("#bg > *");
  $("#root").classList.toggle("has-photos", on);
  if (!photosOn()) { $$("#bg > *").forEach(dropMedia); photoShown = null; }
}
async function loadPhotos() {
  const r = await api("/api/photos").catch(() => null);
  if (!r || !r.photos || !state) return;
  photoTotal = r.photos.length; photoSkipped = r.skipped || 0;
  vids = { ready: r.videos || [], pending: r.pending || 0, failed: r.failed || 0 };
  showStatus();
  const list = [...r.photos.map(i => "photo/" + i), ...vids.ready.map(i => "video/" + i).filter(k => !badVideos.has(k))];
  const key = list.join();
  if (key === photoKey) return;
  const fresh = list.filter(k => k.startsWith("video/") && !photos.includes(k)); // a video that just became ready plays soon
  const first = !photoKey;
  photoKey = key; photos = list; photoQueue = first ? [] : [...shuffled(photos.filter(k => k !== photoShown && !fresh.includes(k))), ...fresh];
  if (photoShown && !photos.includes(photoShown)) photoShown = null;
  const v = bgVideo(); if (v) v.loop = photos.length === 1;
  if (!photoShown) nextPhoto();
  updatePhotoMode();
}
function shuffled(list) {
  const a = [...list];
  for (let i = a.length - 1; i > 0; i--) { const k = Math.floor(Math.random() * (i + 1)); [a[i], a[k]] = [a[k], a[i]]; }
  return a;
}
// loads one picture or video, ready to be shown
async function makeMedia(key) {
  if (!key.startsWith("video/")) {
    const img = new Image();
    img.src = "/" + key;
    await img.decode();
    return img;
  }
  const v = document.createElement("video");
  v.muted = true; v.playsInline = true; v.preload = "auto"; v.disableRemotePlayback = true;
  v.loop = photos.length === 1;
  try {
    await new Promise((ok, no) => {
      v.onloadeddata = ok; v.onerror = () => no(new Error("video")); setTimeout(() => no(new Error("slow")), 30000);
      v.src = "/" + key;
    });
    await v.play();
  } catch (e) { badVideos.add(key); dropMedia(v); throw e; }
  v.onended = () => { if (v === bgVideo()) nextPhoto(); };
  v.onerror = () => { badVideos.add(key); photos = photos.filter(p => p !== key); if (v === bgVideo()) { photoShown = null; nextPhoto(); } };
  return v;
}
async function nextPhoto() {
  if (photoBusy || !photosOn()) return;
  photoBusy = true;
  try {
    for (let tries = Math.min(photos.length, 5); tries > 0; tries--) {
      if (!photoQueue.length) {
        photoQueue = shuffled(photos);
        if (photoQueue.length > 1 && photoQueue[photoQueue.length - 1] === photoShown) photoQueue.reverse();
      }
      const key = photoQueue.pop();
      if (!photos.includes(key)) { tries++; if (!photos.length) break; continue; }
      if (key === photoShown) break; // only one picture: leave it up
      let el;
      try { el = await makeMedia(key); } catch (e) { photos = photos.filter(p => p !== key); continue; } // unreadable: skip it
      if (!photosOn()) { dropMedia(el); break; }
      const root = $("#root"), bg = $("#bg"), old = [...bg.children];
      const w = el.naturalWidth || el.videoWidth, h = el.naturalHeight || el.videoHeight;
      // fill the screen, but never crop a tall photo to a strip on a wide screen (or the reverse)
      el.className = (w >= h) === (root.clientWidth >= root.clientHeight) ? "" : "whole";
      bg.append(el);
      el.offsetWidth; // start the fade from invisible
      el.classList.add("on");
      old.forEach(o => { if (o.tagName === "VIDEO") o.pause(); }); // never two at once
      setTimeout(() => old.forEach(dropMedia), 1200);
      photoShown = key;
      break;
    }
  } finally { photoBusy = false; photoAt = Date.now(); stall = { t: -1, at: Date.now() }; if (state) updatePhotoMode(); }
}
let stall = { t: -1, at: 0 };
setInterval(() => {
  if (!state || !photosOn()) return;
  const v = bgVideo(), dim = $("#dim").classList.contains("on");
  if (v) { // a video plays to its end, or as long as the settings allow
    if (dim) { if (!v.paused) v.pause(); stall.at = Date.now(); return; }
    if (v.paused && !v.ended) v.play().catch(() => {});
    if (v.currentTime !== stall.t) stall = { t: v.currentTime, at: Date.now() };
    else if (v.loop ? false : Date.now() - stall.at > 20000) return nextPhoto(); // stuck
    if (!v.loop && Date.now() - photoAt > (S().videoSeconds + 15) * 2000) nextPhoto();
    return;
  }
  if (!dim && Date.now() - photoAt >= S().photoSeconds * 1000) nextPhoto();
}, 1000);
setInterval(loadPhotos, 5 * 60000);

/* ---------------- night dimming ---------------- */
function inNight() {
  const s = S(); if (!s.nightDim) return false;
  const m = d => { const [h, mi] = (d || "0:0").split(":").map(Number); return h * 60 + (mi || 0); };
  const n = new Date(), cur = n.getHours() * 60 + n.getMinutes(), a = m(s.nightStart), b = m(s.nightEnd);
  return a <= b ? cur >= a && cur < b : cur >= a || cur < b;
}
function updateDim() { $("#dim").classList.toggle("on", inNight() && Date.now() > dimOverride); }

/* ---------------- data loading ---------------- */
function setCal(c) {
  if (c.error || !c.events) return;
  // say so when something was added, moved or removed
  const was = cal.day === c.day && JSON.stringify(cal.events) !== JSON.stringify(c.events) && cal.checked;
  cal = c; checked = c.checked || checked;
  showStatus();
  if (was) toast("Calendar updated");
}
async function loadCal() {
  const c = await api("/api/calendar").catch(() => null);
  if (c) setCal(c);
  if (state) render();
}
async function loadData() {
  const [c, w] = await Promise.allSettled([api("/api/calendar"), api("/api/weather")]);
  if (c.status === "fulfilled") setCal(c.value);
  if (w.status === "fulfilled" && (!w.value.error || !wx || wx.error)) wx = w.value;
  if (state) { render(); dropSplash(); }
}
async function refreshNow() {
  if (refreshNow.busy) return;
  refreshNow.busy = true;
  toast("Checking the calendar…");
  try {
    const before = JSON.stringify(cal.events), r = await api("/api/refresh", {});
    await loadData();
    if (JSON.stringify(cal.events) === before) toast(r.ok ? "Calendar is up to date" : "Could not reach the calendar");
  } catch (e) { toast("Could not check the calendar"); }
  refreshNow.busy = false;
}
// bottom right: when the calendar was last read, so it is clear it keeps up
let checked = null, preparing = false;
function showStatus() {
  if (!state) return;
  const t = [preparing && vids.pending ? `Preparing ${vids.pending} video${vids.pending > 1 ? "s" : ""}` : "", checked ? "Calendar checked " + fmtTime(new Date(checked * 1000)) : ""].filter(Boolean).join(" · ");
  if ($("#stRight").textContent !== t) $("#stRight").textContent = t;
}
async function poll() {
  for (;;) {
    try {
      const st = await api("/api/state?since=" + (state ? state.version : 0));
      const prev = state;
      if (prev && prev.version === st.version) continue; // nothing new
      state = st;
      if (lastCmd === null) lastCmd = st.command.id;
      else if (st.command.id !== lastCmd) { lastCmd = st.command.id; doAction(st.command.action, st.command.value); }
      applySettings();
      if (prev && prev.rev.media !== st.rev.media) loadPhotos();
      if (!prev || JSON.stringify(prev.settings.feeds) !== JSON.stringify(st.settings.feeds) || JSON.stringify(prev.settings.location) !== JSON.stringify(st.settings.location) || prev.settings.units !== st.settings.units) loadData();
      else if (prev.rev.cal !== st.rev.cal) loadCal();
      else render();
    } catch (e) { await new Promise(r => setTimeout(r, 5000)); }
  }
}

/* ---------------- splash ---------------- */
function dropSplash() { const s = $("#splash"); if (s) s.remove(); }
setTimeout(dropSplash, 20000); // never in the way for long

/* ---------------- timers ---------------- */
let lastMinute = -1, lastDay = ymd(new Date());
setInterval(() => {
  if (!state) return;
  const n = new Date();
  if (S().showSeconds || n.getMinutes() !== lastMinute) {
    lastMinute = n.getMinutes();
    if (ymd(n) !== lastDay) { lastDay = ymd(n); loadData(); }
    if (!$$(".overlay.open").length) render();
    updateDim();
  }
  const ac = +S().autocycle || 0, bar = $("#cycleBar");
  if (ac > 0 && Date.now() > pausedUntil && !$$(".overlay.open").length) {
    const p = (Date.now() - cycleStart) / (ac * 1000);
    bar.style.transform = `scaleX(${Math.min(1, p)})`;
    if (p >= 1) go(1, false);
  } else { if (bar.style.transform) bar.style.transform = ""; if (ac > 0) cycleStart = Date.now(); }
}, 1000);
setInterval(loadData, 10 * 60000);
// no internet yet after switching on: try the weather again soon
setInterval(() => { if (state && (!wx || (wx.error && !/^(no-location|location-not-found)$/.test(wx.error)))) loadData(); }, 60000);

/* ---------------- heartbeat ----------------
   Tells the Pi's watchdog the screen is really being drawn. It only fires
   from inside a drawn frame (one nearly invisible dot changes), so a frozen
   display stops it and the watchdog restarts the screen. */
let beat = 0;
function heartbeat() {
  requestAnimationFrame(() => {
    $("#beat").style.background = (beat ^= 1) ? "#000" : "#010101";
    requestAnimationFrame(() => requestAnimationFrame(() => report().catch(() => {})));
  });
}
async function report() {
  let q = null;
  try { const v = bgVideo(); if (v && !v.paused) q = v.getVideoPlaybackQuality(); } catch (e) { /* the report must go out */ }
  const r = await api("/api/alive", { video: q ? { shown: q.totalVideoFrames, dropped: q.droppedVideoFrames } : null, badVideos: badVideos.size });
  checked = r.checked || checked; preparing = r.busy;
  showStatus();
}
if (params.has("kiosk")) { heartbeat(); setInterval(heartbeat, 20000); }

(async function init() {
  poll();
  const wait = () => { if (!state) return setTimeout(wait, 200); loadPhotos(); };
  wait();
})();

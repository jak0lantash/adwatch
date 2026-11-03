/* AdWatch front end. Everything app-specific comes from the fork's config (window.ADWATCH_CONFIG);
   a fork can add formatters in fork.js:  AdWatch.formats.myformat = (value, ad, field) => "<b>html</b>"; */
(function () {
const CFG = window.ADWATCH_CONFIG;
const FIELDS = Object.fromEntries(CFG.fields.map(f => [f.key, f]));
const SITES = Object.fromEntries(CFG.sites.map(s => [s.id, s]));
const MULTI_SITE = CFG.sites.length > 1;
const LOW_WATER = CFG.low_water;

const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const eur = n => n == null ? "" : "€" + Math.round(Number(n)).toLocaleString("en-IE");
const ago = iso => {
  if (!iso) return "";
  const d = (Date.now() - new Date(iso)) / 36e5;
  return d < 1 ? "just now" : d < 24 ? `${Math.round(d)} h ago` : `${Math.round(d / 24)} days ago`;
};
const kms = n => n == null ? "" : (n < 10 ? n.toFixed(1) : Math.round(n)) + " km";
const hhmm = iso => new Date(iso).toLocaleTimeString("en-IE", { hour: "2-digit", minute: "2-digit" });
const cap = s => s ? s[0].toUpperCase() + s.slice(1) : s;
const plural = n => n === 1 ? CFG.item : CFG.items;

const ICON_DOWN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H6.7a2 2 0 0 0-2 1.7l-1.4 9A2 2 0 0 0 5.3 15H10zM17 2h2.7A2.3 2.3 0 0 1 22 4v7a2.3 2.3 0 0 1-2.3 2H17"/></svg>`;
const ICON_SHARE = `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px"><path d="M12 3v12M7 8l5-5 5 5"/><path d="M5 13v6a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-6"/></svg>`;

for (const [k, v] of Object.entries(CFG.theme || {})) document.documentElement.style.setProperty(k, v);

/* ---------- values and templates ---------- */
const formats = {
  eur: v => esc(eur(v)),
  km: v => esc(kms(Number(v))),
  ago: v => esc(ago(v)),
};

// Plain-text value of a field (used in templates, share text, rows without a formatter).
function text(key, ad) {
  const v = ad[key];
  if (key === "price") return ad.price ? eur(ad.price) : (ad.price_text || "POA");
  if (key === "distance") return ad.km == null ? "" : kms(ad.km);
  if (key === "site") return SITES[ad.site]?.label || "";
  if (key === "published" || key === "first_seen") return ago(v);
  if (v == null || v === "") return "";
  const f = FIELDS[key] || {};
  let s;
  if (f.format === "eur") s = eur(v);
  else if (f.format === "ago") s = ago(v);
  else if (f.type === "decimal") s = Number(v).toFixed(f.decimals ?? 1);
  else if (f.type === "int") s = f.grouping === false ? String(Math.round(Number(v))) : Number(v).toLocaleString("en-IE");
  else s = String(v);
  return (f.prefix || "") + s + (f.unit || "");
}

function html(key, ad) {
  const f = FIELDS[key];
  const fmt = f && f.format && AdWatch.formats[f.format];
  if (fmt && ad[key] != null && ad[key] !== "") return fmt(ad[key], ad, f);
  return esc(text(key, ad));
}

// "{year} {make} {model}" -> "2020 Audi A6". Text in [brackets] is dropped if a field in it is missing.
function tpl(t, ad) {
  if (!t) return "";
  const fill = s => s.replace(/\{(\w+)\}/g, (_, k) => text(k, ad));
  let out = t.replace(/\[([^\]]*)\]/g, (_, inner) => {
    const keys = [...inner.matchAll(/\{(\w+)\}/g)].map(m => m[1]);
    return keys.every(k => text(k, ad) !== "") ? fill(inner) : "";
  });
  out = fill(out).replace(/\s+/g, " ").trim();
  for (let i = 0; i < 4; i++) out = out.replace(/^[·\/,|]\s*/, "").replace(/\s*[·\/,|]$/, "").replace(/([·\/,|])\s*[·\/,|]/g, "$1");
  return out.trim();
}

const photoUrl = ad => {
  try {
    const h = new URL(ad.image).hostname;
    if (CFG.sites.some(s => (s.photo_hosts || []).includes(h))) return "/img?u=" + encodeURIComponent(ad.image);
  } catch (e) {}
  return ad.image;
};
const siteLabel = ad => SITES[ad.site]?.label || "the site";

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

const S = { tab: "market", ads: [], searches: [], searchFilter: null, history: [], status: null, more: false, loading: false };

/* ---------- toast ---------- */
let toastTimer;
function toast(msg, undoFn) {
  document.querySelector(".toast")?.remove();
  const t = document.createElement("div");
  t.className = "toast"; t.setAttribute("role", "status");
  t.innerHTML = `<span>${esc(msg)}</span>${undoFn ? "<button>Undo</button>" : ""}`;
  if (undoFn) t.querySelector("button").onclick = () => { t.remove(); undoFn(); };
  document.body.append(t);
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.remove(), 5000);
}

/* ---------- status ---------- */
function fetchText() {
  for (const [id, st] of Object.entries(S.status?.sites || {})) {
    const label = SITES[id]?.label || id;
    if (st.blocked_until && new Date(st.blocked_until) > Date.now()) return `${label} asked to slow down; next try at ${hhmm(st.blocked_until)}`;
    if (st.doing) return `${label}: ${st.doing}…`;
    if (st.queued) return `Waiting a moment before asking ${label}…`;
  }
  return "";
}
async function refreshStatus() {
  try {
    S.status = await api("/api/status");
    for (const k of ["market", "liked", "disliked"]) $("#c-" + k).textContent = S.status.counts[k] || "";
    $("#run-status").textContent = CFG.demo ? "Demo data" : fetchText()
      || (S.status.last_refresh ? `Updated ${ago(S.status.last_refresh)}` : "");
  } catch (e) { $("#run-status").textContent = e.message; }
}

/* ---------- tabs ---------- */
document.querySelectorAll("nav button").forEach(b => b.onclick = () => go(b.dataset.tab));
async function go(tab) {
  S.tab = tab;
  document.querySelectorAll("nav button").forEach(b =>
    b.dataset.tab === tab ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current"));
  if (tab === "settings") return renderSettings();
  const qs = new URLSearchParams({ list: tab });
  if (tab === "market" && S.searchFilter) qs.set("search_id", S.searchFilter);
  const [r, searches] = await Promise.all([api("/api/ads?" + qs), api("/api/searches")]);
  S.ads = r.ads; S.searches = searches; S.more = !!r.more; S.loading = !!r.loading;
  tab === "market" ? renderDeck() : renderList();
  await refreshStatus();
  if (tab === "market") scheduleRefill();
}

/* Ask for more ads (the server fetches the next page if one is needed) and append new ones
   to the deck without reshuffling it. Keeps polling while a page is on its way. */
let refillTimer = null;
function scheduleRefill(delay = 4000) {
  clearTimeout(refillTimer);
  if (S.tab !== "market") return;
  if (S.ads.length > LOW_WATER && !S.loading) return;
  if (!S.more && !S.loading) return;
  refillTimer = setTimeout(refill, delay);
}
async function refill() {
  if (S.tab !== "market") return;
  const qs = new URLSearchParams({ list: "market" });
  if (S.searchFilter) qs.set("search_id", S.searchFilter);
  let r;
  try { r = await api("/api/ads?" + qs); } catch (e) { return scheduleRefill(15000); }
  if (S.tab !== "market") return;
  const known = new Set([...S.ads, ...S.history].map(a => a.id));
  const fresh = r.ads.filter(a => !known.has(a.id));
  const wasEmpty = !S.ads.length;
  S.ads.push(...fresh); S.more = !!r.more; S.loading = !!r.loading;
  await refreshStatus();
  if (wasEmpty || !S.ads.length) renderDeck();
  else if (fresh.length && $(".remaining")) $(".remaining").innerHTML = remainingText();
  scheduleRefill();
}
function remainingText() {
  const n = S.ads.length;
  return `${n} ${plural(n)} to review${S.more ? " so far (more to come)" : ""} · swipe or use ← →`;
}

/* ---------- market deck ---------- */
const SPECIAL_ROWS = {
  distance: ad => ad.km == null ? null : ["Distance", esc(`${kms(ad.km)} from ${CFG.centre?.name || "the centre"}`)],
  site: ad => ["Site", esc(siteLabel(ad))],
  searches: ad => (ad.searches || []).length ? ["Search", esc(ad.searches.join(", "))] : null,
  published: ad => ad.published ? ["Listed", esc(ago(ad.published))] : null,
  first_seen: ad => ["First seen", esc(ago(ad.first_seen))],
  price: ad => ["Price", esc(text("price", ad))],
};
function specRows(ad) {
  return (CFG.card.rows || []).map(spec => {
    const r = typeof spec === "string" ? { key: spec } : spec;
    let label, value, excl = null;
    if (SPECIAL_ROWS[r.key] && !r.template) {
      const out = SPECIAL_ROWS[r.key](ad); if (!out) return "";
      [label, value] = out;
    } else {
      const f = FIELDS[r.key] || {};
      label = r.label || f.label || cap(r.key);
      value = r.template ? esc(tpl(r.template, ad)) : html(r.key, ad);
      if (r.hide_if && tpl(r.hide_if, ad) === text(r.key, ad)) return "";
      if (f.exclude && ad[r.key] != null && ad[r.key] !== "") excl = { field: r.key, value: String(ad[r.key]), shown: text(r.key, ad) };
    }
    if (!value) return "";
    return `<div class="row"><dt>${esc(label)}</dt><dd>${value}</dd>${excl
      ? `<button class="thumbdown" data-field="${esc(excl.field)}" data-value="${esc(excl.value)}" data-label="${esc(label)}" data-shown="${esc(excl.shown)}" aria-label="Never show ${esc(label)}: ${esc(excl.shown)}" title="Never show ${esc(label)}: ${esc(excl.shown)}">${ICON_DOWN}</button>`
      : "<span></span>"}</div>`;
  }).join("");
}

function priceBits(ad) {
  const h = ad.price_history || [];
  const first = h.length ? h[0][1] : null;
  return { dropped: first && ad.price && ad.price < first, first };
}

function valueLine(ad) {
  const c = ad.comp, cmp = CFG.compare;
  if (!c || !cmp) return "";
  const diff = c.value - c.median, pct = Math.round(Math.abs(diff) / c.median * 100);
  const label = tpl(cmp.label || "similar ads", ad);
  let txt, cls;
  if (cmp.per) {
    cls = pct < 5 ? "" : diff < 0 ? "good" : "high";
    txt = pct < 5 ? `About the same price ${cmp.per_label || ""} as` : `${pct}% ${diff < 0 ? "cheaper" : "dearer"} ${cmp.per_label || ""} than`;
    txt += ` the median of ${c.count} ${label} seen so far (${eur(c.median)}${cmp.per_unit || ""})`;
  } else {
    cls = diff < 0 ? "good" : diff > 0 ? "high" : "";
    txt = Math.abs(diff) < (cmp.same_within ?? 250) ? "About the same as" : `${eur(Math.abs(diff))} (${pct}%) ${diff < 0 ? "below" : "above"}`;
    txt += ` the median of ${c.count} ${label} seen so far`;
  }
  return `<p class="value ${cls}">${esc(txt.replace(/\s+/g, " "))}</p>`;
}

const heading = ad => tpl(CFG.card.heading, ad) || ad.title || cap(CFG.item);
const mapUrl = ad => ad.lat != null && ad.lng != null ? `https://www.google.com/maps?q=${ad.lat},${ad.lng}` : null;

function cardHTML(ad) {
  const { dropped, first } = priceBits(ad);
  const map = mapUrl(ad);
  const head = heading(ad);
  const sub = tpl(CFG.card.subtitle, ad);
  const hideSub = !sub || sub.toLowerCase() === head.toLowerCase()
    || (CFG.card.subtitle_hide_if && sub.trim() === tpl(CFG.card.subtitle_hide_if, ad));
  const badge = CFG.card.badge && ad[CFG.card.badge];
  return `
    <div class="photo">
      ${ad.image ? `<img src="${esc(photoUrl(ad))}" alt="" referrerpolicy="no-referrer" loading="eager" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'nophoto',textContent:'No photo'}))">` : `<div class="nophoto">No photo</div>`}
      ${badge ? `<div class="sale">${esc(badge)}</div>` : ""}
      <div class="stamp save">SAVE</div><div class="stamp hide">HIDE</div>
      <div class="sticker">${dropped ? `<s>${eur(first)}</s>` : ""}<b>${esc(text("price", ad))}</b>${tpl(CFG.card.sticker, ad) ? `<small>${esc(tpl(CFG.card.sticker, ad))}</small>` : ""}</div>
    </div>
    <div class="body">
      <h2>${esc(head)}${MULTI_SITE ? `<span class="site">${esc(siteLabel(ad))}</span>` : ""}</h2>
      ${hideSub ? `<div style="height:.55rem"></div>` : `<p class="variant">${esc(sub)}</p>`}
      <dl class="specs">${specRows(ad)}</dl>
      ${valueLine(ad)}
      <div class="links">
        <a class="open-ad" href="${esc(ad.url)}" target="_blank" rel="noopener">Open on ${esc(siteLabel(ad))} ↗</a>
        ${map ? `<a class="open-ad" href="${esc(map)}" target="_blank" rel="noopener">Map ↗</a>` : ""}
      </div>
    </div>`;
}

function renderDeck() {
  const chips = `<div class="chips" role="group" aria-label="Filter by search">
    <button class="chip" aria-pressed="${!S.searchFilter}" data-sid="">All searches</button>
    ${S.searches.filter(s => s.enabled).map(s => `<button class="chip" aria-pressed="${S.searchFilter === s.id}" data-sid="${s.id}">${esc(s.name)}</button>`).join("")}
  </div>`;
  if (!S.ads.length) {
    const noSearches = !S.searches.length;
    const waiting = !noSearches && (S.loading || S.more);
    $("#view").innerHTML = chips + `<div class="empty">
      <h2>${noSearches ? "Add your first search" : waiting ? `Getting ${CFG.items}…` : "You're all caught up"}</h2>
      <p>${noSearches ? `Set up a search on ${esc(CFG.sites.map(s => s.label).join(" or "))}, copy its address, and paste it under Searches.`
        : waiting ? esc(fetchText() || "Fetching the next page of results.")
        : `New ${CFG.items} appear here as they're found.`}</p>
      ${noSearches ? `<button class="btn primary" id="add-first">Add a search</button>` : waiting || CFG.demo ? "" : `<button class="btn primary" id="scrape-now">Look for new ${CFG.items} now</button>`}
      ${S.history.length ? ` <button class="btn" id="undo-empty">Undo last swipe</button>` : ""}</div>`;
    bindChips();
    $("#add-first")?.addEventListener("click", () => go("settings"));
    $("#scrape-now")?.addEventListener("click", () => lookNow());
    $("#undo-empty")?.addEventListener("click", undo);
    return;
  }
  const top = S.ads[0], next = S.ads[1];
  $("#view").innerHTML = chips + `
    <div class="deck" id="deck">
      ${next ? `<article class="card under" aria-hidden="true">${cardHTML(next)}</article>` : ""}
      <article class="card" id="top" aria-label="${esc(top.title)}">${cardHTML(top)}</article>
    </div>
    <div class="actions" id="actions">
      <button class="round hide" id="btn-hide" aria-label="Hide this ${esc(CFG.item)}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
      <button class="round undo" id="btn-undo" aria-label="Undo last swipe" ${S.history.length ? "" : "disabled"}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M9 14L4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/></svg></button>
      <button class="round save" id="btn-save" aria-label="Save"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 21s-8-5-8-11.2A4.6 4.6 0 0 1 12 7a4.6 4.6 0 0 1 8 2.8C20 16 12 21 12 21z"/></svg></button>
    </div>
    <p class="remaining">${remainingText()}</p>`;
  bindChips();
  const card = $("#top");
  // The card behind is cut to the front card's height, so a longer ad behind a short one
  // doesn't show its buttons below the front card.
  const fit = () => {
    const front = $("#top"), behind = $(".card.under");
    if (!front) return;
    if (behind) behind.style.height = front.offsetHeight + "px";
    $("#deck").style.minHeight = front.offsetHeight + 16 + "px";
  };
  fit(); document.querySelectorAll(".card img").forEach(i => i.addEventListener("load", fit));
  $("#btn-hide").onclick = () => fling(-1);
  $("#btn-save").onclick = () => fling(1);
  $("#btn-undo").onclick = undo;
  card.querySelectorAll(".thumbdown").forEach(b => b.onclick = e => {
    e.stopPropagation(); exclude(b.dataset.field, b.dataset.value, b.dataset.label, b.dataset.shown);
  });
  enableSwipe(card);
}

function bindChips() {
  document.querySelectorAll(".chip").forEach(c => c.onclick = () => {
    S.searchFilter = c.dataset.sid ? Number(c.dataset.sid) : null; go("market");
  });
}

function enableSwipe(card) {
  let startX = 0, startY = 0, dx = 0, dragging = false, id = null;
  const stamps = { save: $(".stamp.save", card), hide: $(".stamp.hide", card) };
  card.addEventListener("pointerdown", e => {
    if (e.target.closest("button, a")) return;
    dragging = true; id = e.pointerId; startX = e.clientX; startY = e.clientY; dx = 0;
    card.classList.remove("back");
  });
  card.addEventListener("pointermove", e => {
    if (!dragging || e.pointerId !== id) return;
    dx = e.clientX - startX;
    const dy = e.clientY - startY;
    if (Math.abs(dx) < 8 && Math.abs(dy) > 10) { dragging = false; return; } // vertical scroll
    if (Math.abs(dx) > 8) card.setPointerCapture(id);
    card.style.transform = `translateX(${dx}px) rotate(${dx / 18}deg)`;
    stamps.save.style.opacity = Math.max(0, Math.min(1, dx / 110));
    stamps.hide.style.opacity = Math.max(0, Math.min(1, -dx / 110));
  });
  const end = () => {
    if (!dragging) return;
    dragging = false;
    if (Math.abs(dx) > 110) return fling(Math.sign(dx));
    card.classList.add("back"); card.style.transform = "";
    stamps.save.style.opacity = stamps.hide.style.opacity = 0;
  };
  card.addEventListener("pointerup", end);
  card.addEventListener("pointercancel", end);
}

let flinging = false;
async function fling(dir) {
  const card = $("#top"); if (!card || flinging) return;
  flinging = true;
  const ad = S.ads[0];
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  $(dir > 0 ? ".stamp.save" : ".stamp.hide", card).style.opacity = 1;
  card.classList.add("fly");
  card.style.transform = `translateX(${dir * 140}vw) rotate(${dir * 28}deg)`;
  try { await api(`/api/ads/${encodeURIComponent(ad.id)}/status`, { method: "POST", body: { status: dir > 0 ? "liked" : "disliked" } }); }
  catch (e) { toast(e.message); flinging = false; return renderDeck(); }
  S.history.push(ad);
  await new Promise(r => setTimeout(r, reduce ? 0 : 280));
  S.ads.shift(); flinging = false;
  renderDeck(); refreshStatus();
  if (S.ads.length <= LOW_WATER && S.more) { S.loading = true; refill(); }
}

async function undo() {
  const ad = S.history.pop(); if (!ad) return;
  await api(`/api/ads/${encodeURIComponent(ad.id)}/status`, { method: "POST", body: { status: "new" } });
  S.ads.unshift(ad); renderDeck(); refreshStatus();
}

async function exclude(field, value, label, shown) {
  try {
    const r = await api("/api/exclusions", { method: "POST", body: { field, value } });
    const before = S.ads.length;
    await go("market");
    const gone = before - S.ads.length;
    toast(`No more ${label}: ${shown || value}${gone > 1 ? ` (${gone} ${CFG.items} hidden)` : ""}`, async () => {
      if (r.exclusion) await api(`/api/exclusions/${r.exclusion.id}`, { method: "DELETE" });
      go("market");
    });
  } catch (e) { toast(e.message); }
}

async function lookNow(searchId) {
  try { await api("/api/refresh", { method: "POST", body: searchId ? { search_id: searchId } : {} }); }
  catch (e) { return toast(e.message); }
  toast(`Looking for new ${CFG.items}. They'll appear as they're found.`);
  if (S.tab === "market") { S.loading = true; renderDeck(); scheduleRefill(3000); }
  else setTimeout(renderSettings, 3000);
}

document.addEventListener("keydown", e => {
  if (S.tab !== "market" || e.target.closest("input, textarea, select")) return;
  if (e.key === "ArrowRight") fling(1);
  if (e.key === "ArrowLeft") fling(-1);
  if (e.key.toLowerCase() === "z") undo();
});

/* ---------- saved / hidden lists ---------- */
function renderList() {
  const liked = S.tab === "liked";
  if (!S.ads.length) {
    $("#view").innerHTML = `<div class="empty"><h2>${liked ? `No saved ${CFG.items} yet` : "Nothing hidden"}</h2>
      <p>${liked ? "Swipe right on one in Market to keep it here." : "What you swipe left lands here, so you can bring it back."}</p>
      <button class="btn primary" id="to-market">Go to Market</button></div>`;
    $("#to-market").onclick = () => go("market");
    return;
  }
  const shareAll = liked ? `<div class="sharebar"><span>${S.ads.length} saved ${plural(S.ads.length)}</span>
    <button class="btn primary" id="share-all">${ICON_SHARE} Share all</button></div>` : "";
  $("#view").innerHTML = shareAll + `<div class="list">${S.ads.map(ad => {
    const { dropped, first } = priceBits(ad);
    const site = SITES[ad.site] || {};
    const seenDays = (Date.now() - new Date(ad.last_seen)) / 864e5;
    const badge = CFG.card.badge && ad[CFG.card.badge];
    const ber = CFG.card.list_badge ? html(CFG.card.list_badge, ad) : "";
    return `<article class="item">
      ${ad.image ? `<img src="${esc(photoUrl(ad))}" alt="" referrerpolicy="no-referrer" loading="lazy">` : `<div class="nophoto"></div>`}
      <div class="info">
        <h3>${esc(tpl(CFG.card.list_heading || CFG.card.heading, ad) || ad.title)}</h3>
        <div class="meta">${esc(tpl(CFG.card.list_meta, ad))}</div>
        <span class="price">${esc(text("price", ad))}</span> ${ber}
        ${dropped ? `<span class="tag drop">was ${eur(first)}</span>` : ""}
        ${badge ? `<span class="tag">${esc(badge)}</span>` : ""}
        ${!ad.active ? `<span class="tag gone">No longer listed</span>`
          : liked && ad.checked_at ? `<span class="tag">Rechecked ${ago(ad.checked_at)}</span>`
          : site.mode === "on_demand" && seenDays > 3 ? `<span class="tag">last seen on ${esc(siteLabel(ad))} ${ago(ad.last_seen)}</span>` : ""}
        <div class="btns">
          <a class="btn primary" href="${esc(ad.url)}" target="_blank" rel="noopener">Open on ${esc(siteLabel(ad))} ↗</a>
          ${liked ? `<button class="btn" data-share="${esc(ad.id)}">${ICON_SHARE} Share</button>` : ""}
          ${liked
            ? `<button class="btn" data-id="${esc(ad.id)}" data-to="new">Back to market</button><button class="btn danger" data-id="${esc(ad.id)}" data-to="disliked">Hide</button>`
            : `<button class="btn" data-id="${esc(ad.id)}" data-to="new">Restore to market</button><button class="btn" data-id="${esc(ad.id)}" data-to="liked">Save</button>`}
        </div>
      </div></article>`;
  }).join("")}</div>`;
  document.querySelectorAll("[data-share]").forEach(b => b.onclick = () => share([S.ads.find(a => a.id === b.dataset.share)]));
  $("#share-all")?.addEventListener("click", () => share(S.ads));
  document.querySelectorAll(".btns button[data-to]").forEach(b => b.onclick = async () => {
    await api(`/api/ads/${encodeURIComponent(b.dataset.id)}/status`, { method: "POST", body: { status: b.dataset.to } });
    toast(b.dataset.to === "new" ? "Moved back to Market" : b.dataset.to === "liked" ? "Saved" : "Hidden");
    go(S.tab);
  });
}

/* ---------- sharing saved ads ---------- */
const shareLine = ad => `${tpl(CFG.card.share, ad) || ad.title}\n${ad.url}`;
async function share(ads) {
  ads = ads.filter(a => a && a.url);
  if (!ads.length) return;
  const all = ads.map(shareLine).join("\n\n");
  const one = ads.length === 1;
  // Phones: the system share sheet (WhatsApp, Messages, email…). Else: copy to clipboard.
  if (navigator.share) {
    try {
      const first = shareLine(ads[0]).split("\n")[0];
      await navigator.share(one ? { title: first, text: first, url: ads[0].url } : { title: CFG.share_title, text: all });
      return;
    } catch (e) { if (e.name === "AbortError") return; }
  }
  try {
    await navigator.clipboard.writeText(all);
    toast(one ? "Link copied" : `${ads.length} links copied. Paste them into a message.`);
  } catch (e) {
    const box = document.createElement("div");  // last resort: show the text to copy by hand
    box.className = "toast"; box.style.flexDirection = "column"; box.style.alignItems = "stretch";
    box.innerHTML = `<textarea rows="8" style="width:min(80vw,28rem);font:inherit;font-size:.85rem">${esc(all)}</textarea><button>Done</button>`;
    box.querySelector("button").onclick = () => box.remove();
    document.querySelector(".toast")?.remove(); document.body.append(box);
    box.querySelector("textarea").select();
  }
}

/* ---------- searches & exclusions ---------- */
// Optional refinements, from the fields that declare a "filter".
function filterInputs() {
  const out = [["price", "min", "Min price €"], ["price", "max", "Max price €"]];
  for (const f of CFG.fields) {
    if (!f.filter) continue;
    const u = f.unit ? ` (${f.unit.trim()})` : "";
    if (f.filter === "range") out.push([f.key, "min", `${f.label} from${u}`], [f.key, "max", `${f.label} to${u}`]);
    if (f.filter === "min") out.push([f.key, "min", `Min ${f.label.toLowerCase()}${u}`]);
    if (f.filter === "max") out.push([f.key, "max", `Max ${f.label.toLowerCase()}${u}`]);
    if (f.filter === "contains") out.push([f.key, "contains", `${f.label} contains`]);
  }
  if (CFG.centre) out.push(["distance", "max", `Max km from ${CFG.centre.name}`]);
  return out;
}
function filterSummary(filters) {
  const bits = filterInputs().filter(([k, kind]) => filters[k]?.[kind] != null && filters[k][kind] !== "")
    .map(([k, kind, label]) => `${label}: ${k === "price" ? eur(filters[k][kind]) : filters[k][kind]}`);
  if (filters.side) bits.push(`only ${CFG.centre?.name || "the centre"} and ${filters.side} of it`);
  return bits.join(", ");
}
function searchForm(s = {}) {
  const f = s.filters || {};
  const hints = CFG.sites.map(x => x.url_hint).filter(Boolean);
  const anyFilter = Object.keys(f).length > 0;
  return `<form class="grid" data-id="${s.id || ""}">
    <label class="wide">Name<input type="text" name="name" required value="${esc(s.name)}" placeholder="What you're looking for"></label>
    <label class="wide">Search address (copy it from ${esc(CFG.sites.map(x => x.label).join(" or "))})<input type="url" name="url" required value="${esc(s.url)}" placeholder="${esc(hints[0] || "https://…")}"></label>
    <details class="more"${anyFilter ? " open" : ""}><summary>Refine further (optional)</summary>
      <div class="grid">
      ${filterInputs().map(([k, kind, label]) => `<label>${esc(label)}<input type="${kind === "contains" ? "text" : "number"}" ${kind === "contains" ? "" : 'step="any"'} data-f="${k}" data-kind="${kind}" value="${esc(f[k]?.[kind])}"></label>`).join("")}
      ${CFG.centre ? `<label class="wide">Direction<select data-side>
        <option value="">Any direction from ${esc(CFG.centre.name)}</option>
        ${["west", "east", "north", "south"].map(d => `<option value="${d}" ${f.side === d ? "selected" : ""}>Only ${esc(CFG.centre.name)} and ${d} of it</option>`).join("")}
      </select></label>` : ""}
      </div>
      <p class="hint">For things the site can't filter on. Several values: separate with commas.</p>
    </details>
    <div class="formbtns"><button class="btn primary" type="submit">${s.id ? "Save changes" : "Add search"}</button>
    ${s.id ? `<button class="btn" type="button" data-cancel>Cancel</button>` : ""}</div>
  </form>`;
}
function readForm(form) {
  const filters = {};
  form.querySelectorAll("[data-f]").forEach(i => {
    if (i.value === "") return;
    (filters[i.dataset.f] ||= {})[i.dataset.kind] = i.dataset.kind === "contains" ? i.value : Number(i.value);
  });
  const side = form.querySelector("[data-side]")?.value;
  if (side) filters.side = side;
  return { name: form.name.value.trim(), url: form.url.value.trim(), filters };
}

function searchStatus(s) {
  const site = SITES[s.site];
  if (!site) return "No site recognised for this address";
  const bits = [MULTI_SITE ? site.label : "", s.enabled ? "" : "Paused"];
  if (site.mode === "on_demand") {
    bits.push(s.total != null ? `${s.total} results${s.exhausted ? ", all fetched" : ""}` : "Loads when you open the Market");
    if (s.refreshed_at) bits.push(`page 1 checked ${ago(s.refreshed_at)}`);
  } else {
    bits.push(s.last_run ? `Last check ${ago(s.last_run)}: ${s.last_count ?? 0} ${CFG.items}` : "Not checked yet");
  }
  return bits.filter(Boolean).join(" · ");
}

function howItChecks(st) {
  if (CFG.demo) return `<p class="hint">Demo mode is on: nothing is fetched from any website.</p>`;
  const out = CFG.sites.map(site => {
    const s = st.sites[site.id] || {};
    const how = site.mode === "on_demand"
      ? `asked only while you use the Market: page 1 of each search when you open it (again after ${site.refresh_hours} hours), then the next page when you're down to your last ${LOW_WATER}.${site.min_gap >= 1 ? ` At most one request every ${Math.round(site.min_gap)} seconds or so.` : ""}`
      : `checked every ${site.interval_hours} hours, every page of each search.`;
    return `<p class="hint"><b>${esc(site.label)}</b> is ${how}${s.error ? `<br><span style="color:var(--hide)">Last problem: ${esc(s.error)}</span>` : ""}</p>`;
  });
  if (CFG.sites.some(s => s.can_check)) {
    out.push(`<p class="hint"><b>Saved ${CFG.items}</b> are rechecked on their own page about every ${Math.round(CFG.check_saved_hours / 24 * 10) / 10} days, a few at a time, to catch price changes and ones that have gone.${st.checker?.last_checked ? ` Last recheck ${ago(st.checker.last_checked)}.` : ""}</p>`);
  }
  out.push(`<p class="hint">If a site asks to slow down, ${esc(CFG.name)} waits 5 minutes (longer if it happens again).</p>`);
  const now = fetchText();
  if (now) out.push(`<p class="hint"><b>${esc(now)}</b></p>`);
  return out.join("");
}

async function renderSettings() {
  await refreshStatus();
  const [searches, excl] = await Promise.all([api("/api/searches"), api("/api/exclusions")]);
  const st = S.status;
  $("#view").innerHTML = `
    <section class="panel">
      <h2>Searches</h2>
      <p class="hint">Set up a search on ${esc(CFG.sites.map(s => s.label).join(" or "))} with its own filters, copy the address from your browser, and paste it here with a name.</p>
      ${searches.map(s => `<div class="search" data-id="${s.id}">
        <div class="search-head"><strong>${esc(s.name)}</strong><span class="sub">${esc(searchStatus(s))}</span></div>
        ${s.last_error ? `<div class="err">${esc(s.last_error)}</div>` : ""}
        ${filterSummary(s.filters) ? `<div class="sub">${esc(filterSummary(s.filters))}</div>` : ""}
        <div class="btns">
          <a class="btn" href="${esc(s.url)}" target="_blank" rel="noopener">Open on ${esc(SITES[s.site]?.label || "the site")} ↗</a>
          <button class="btn" data-act="edit">Edit</button>
          <button class="btn" data-act="toggle">${s.enabled ? "Pause" : "Resume"}</button>
          ${CFG.demo ? "" : `<button class="btn" data-act="run">Look for new ${CFG.items}</button>`}
          <button class="btn danger" data-act="delete">Delete</button>
        </div>
        <div class="editor"></div>
      </div>`).join("")}
      <h2 style="margin-top:1rem">New search</h2>
      ${searchForm()}
    </section>

    <section class="panel">
      <h2>Never show</h2>
      <p class="hint">Tap the thumbs-down beside a detail on any card to add it here.</p>
      <div class="excl">${excl.length ? excl.map(e => `<span>${esc(FIELDS[e.field]?.label || e.field)}: ${esc(text(e.field, { [e.field]: e.value }))}<button data-eid="${e.id}" aria-label="Show ${esc(e.value)} again">✕</button></span>`).join("") : `<span style="padding-right:.7rem">Nothing excluded yet</span>`}</div>
    </section>

    <section class="panel">
      <h2>How ${esc(CFG.name)} looks for ${esc(CFG.items)}</h2>
      ${howItChecks(st)}
      <div class="btns">${CFG.demo ? "" : `<button class="btn primary" id="scrape-all">Look for new ${CFG.items} now</button>`}
      <button class="btn" id="show-debug">Show what the scraper saw</button></div>
      <div id="debug" class="debug"></div>
    </section>`;

  document.querySelectorAll("form.grid").forEach(bindForm);
  function bindForm(f) {
    f.onsubmit = async e => {
      e.preventDefault();
      try {
        const body = readForm(f);
        if (f.dataset.id) await api(`/api/searches/${f.dataset.id}`, { method: "PUT", body });
        else await api("/api/searches", { method: "POST", body });
        toast(f.dataset.id ? "Search updated" : `Search added. Its ${CFG.items} appear in Market.`);
        renderSettings();
      } catch (err) { toast(err.message); }
    };
    f.querySelector("[data-cancel]")?.addEventListener("click", () => renderSettings());
  }
  document.querySelectorAll(".search").forEach(el => {
    const s = searches.find(x => x.id == el.dataset.id);
    el.querySelectorAll("[data-act]").forEach(b => b.onclick = async () => {
      const act = b.dataset.act;
      if (act === "edit") { $(".editor", el).innerHTML = searchForm(s); bindForm($(".editor form", el)); }
      if (act === "toggle") { await api(`/api/searches/${s.id}`, { method: "PUT", body: { enabled: !s.enabled } }); renderSettings(); }
      if (act === "delete" && confirm(`Delete "${s.name}"? Its ${CFG.items} stay in Saved and Hidden.`)) { await api(`/api/searches/${s.id}`, { method: "DELETE" }); renderSettings(); }
      if (act === "run") lookNow(s.id);
    });
  });
  document.querySelectorAll("[data-eid]").forEach(b => b.onclick = async () => {
    await api(`/api/exclusions/${b.dataset.eid}`, { method: "DELETE" }); renderSettings();
  });
  $("#scrape-all")?.addEventListener("click", () => lookNow());
  $("#show-debug").onclick = async () => {
    const d = await api("/api/debug");
    $("#debug").innerHTML = d.length ? d.map(x => `<h3>${esc(x.search)}</h3>
      <p class="hint"><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.url)}</a><br>Fetched ${ago(x.fetched)}. Recognised on page 1: ${x.parsed_count}${x.total != null ? `, total reported: ${esc(x.total)}` : ""}.${x.title ? ` Page title: ${esc(x.title)}.` : ""}</p>
      ${x.screenshot ? `<details><summary>Screenshot</summary><img src="/api/debug/${esc(x.name)}.png" alt="Screenshot of the page"></details>` : ""}
      <details><summary>Recognised listings</summary><pre class="debug">${esc(JSON.stringify(x.parsed_ads, null, 2))}</pre></details>
      ${x.raw ? `<details><summary>Raw data (for fixing the parser)</summary><pre class="debug">${esc(JSON.stringify(x.raw, null, 2))}</pre></details>` : ""}`).join("")
      : `<p class="hint">Nothing yet. Run a search first.</p>`;
  };
}

window.AdWatch = {
  config: CFG, formats, esc, eur, ago, text, tpl,
  start() { go("market"); setInterval(refreshStatus, 60000); },
};
})();

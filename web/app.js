// Enjoyable Lite Web: paste a YouTube link, hear what happens on screen in the pauses between the speech.
// Cloud Run prepares the descriptions just ahead of the viewer; they arrive live from Firestore (or /api/cues locally).
const CFG = window.ENJOYABLE || {};
const API = CFG.api || "";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

// videos prepared in advance (exact timing): they start at once
const DEMOS = CFG.demos || [];

// ---------- settings (this browser only) ----------
const DEFAULTS = { look: "listen", lang: "", rate: 1.0, vol: 1.0, vvol: 1.0, dvol: 1.0, mute: false, duck: "0.5",
                   dipVoice: true, dipDub: true, captions: false, speak: true, on: true, dub: true,
                   local: false, welcomed: false };
let S = { ...DEFAULTS };
try { Object.assign(S, JSON.parse(localStorage.getItem("enjoyable") || "{}")); } catch { /* private window */ }
function save(patch) {
  Object.assign(S, patch);
  try { localStorage.setItem("enjoyable", JSON.stringify(S)); } catch { /* ignore */ }
  applyLook();
}
let LANGS = [];
const langOf = (c) => LANGS.find((l) => l[0] === c);
function systemLanguage() {
  for (const tag of navigator.languages || [navigator.language || "en"]) {
    const c = tag.toLowerCase().split("-")[0];
    if (langOf(c)) return c;
  }
  return "en";
}
function applyLook() {
  document.documentElement.dataset.look = S.look;
  document.documentElement.lang = "en";
}

// ---------- speaking to the viewer ----------
const live = $("#live");
function announce(text) { live.textContent = ""; setTimeout(() => { live.textContent = text; }, 30); }
const voice = new Audio();
const dubVoice = new Audio();
voice.preservesPitch = dubVoice.preservesPitch = true;
const ducking = { voice: false, dub: false };
// The video's sound eases down when a voice starts and back up when it ends (S-curve, no audible step). A timer, not
// animation frames, so it still completes when the tab is in the background.
let volNow = null;
let volTimer = 0;
function fadeVolume(target, ms) {
  clearInterval(volTimer);
  if (volNow === null || ms <= 0) { volNow = target; player.setVolume(Math.round(target)); return; }
  const from = volNow;
  const t0 = performance.now();
  volTimer = setInterval(() => {
    const u = Math.min(1, (performance.now() - t0) / ms);
    volNow = from + (target - from) * u * u * (3 - 2 * u);
    player.setVolume(Math.round(volNow));
    if (u >= 1) clearInterval(volTimer);
  }, 16);
}
function duck(which, on, instant = false) {
  ducking[which] = on;
  if (!player?.setVolume) return;
  const underVoice = ducking.voice && S.dipVoice ? Number(S.duck) : 1;     // each voice: the viewer's own choice
  const underDub = ducking.dub && S.dipDub ? Math.min(0.2, Number(S.duck)) : 1;
  const level = Math.min(underVoice, underDub);
  const target = 100 * level * (S.mute ? 0 : Number(S.vvol));              // the viewer's own video volume, dipped
  fadeVolume(target, instant ? 0 : target < (volNow ?? target) ? 200 : 450);
}
let sayToken = 0;
async function say(text, { force = false } = {}) {
  announce(text);
  if (!S.speak && !force) return;
  const my = ++sayToken;
  try {
    const r = await fetch(API + "/api/say", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ text, rate: S.rate }) });
    const { audio_url } = await r.json();
    if (my !== sayToken) return;
    playVoice(audio_url, { rate: 1, duck: false });
  } catch { /* the screen reader still has the live region */ }
}
function playVoice(url, { rate = S.rate, duck: dip = true, onend, el = voice } = {}) {
  const which = el === dubVoice ? "dub" : "voice";
  el.pause();
  el.src = url;
  el.playbackRate = rate;
  el.volume = el === dubVoice ? S.dvol : S.vol;
  duck(which, dip);
  el.onended = el.onerror = () => { duck(which, false); onend?.(); };
  el.play().catch(() => duck(which, false));
}
function hush() { sayToken++; voice.pause(); dubVoice.pause(); duck("voice", false); duck("dub", false); }

// ---------- the voice on this device (opt-in): Kokoro in the browser, WebGPU when there is one ----------
// English only for now; any other language, or while it loads, uses the server's voice clips.
const LOCAL = { tts: null, loading: null, device: "", clips: new Map(), queue: [], busy: false };
const localNote = () => $("#local-note");
const LOCAL_NOTE = "English. Downloads the voice once (about 90–330 MB), then works without the server's voice.";
function loadLocalVoice() {
  if (LOCAL.loading) return LOCAL.loading;
  LOCAL.loading = (async () => {
    const { KokoroTTS } = await import("https://cdn.jsdelivr.net/npm/kokoro-js@1.2.1/dist/kokoro.web.js");
    let gpu = null;
    try { gpu = navigator.gpu && (await navigator.gpu.requestAdapter()); } catch { gpu = null; }
    LOCAL.device = gpu ? "webgpu" : "wasm";
    const seen = {};
    LOCAL.tts = await KokoroTTS.from_pretrained("onnx-community/Kokoro-82M-v1.0-ONNX", {
      dtype: gpu ? "fp32" : "q8", device: LOCAL.device,
      progress_callback: (p) => {
        if (p.status !== "progress" || !p.total) return;
        seen[p.file] = [p.loaded, p.total];
        const [l, t] = Object.values(seen).reduce((a, [x, y]) => [a[0] + x, a[1] + y], [0, 0]);
        localNote().textContent = `Downloading the voice… ${Math.round((100 * l) / t)}% of ${Math.round(t / 1e6)} MB`;
      },
    });
    localNote().textContent = `Ready on this device (${gpu ? "graphics chip" : "processor"}). English videos use it.`;
    announce("The voice on this device is ready.");
    return LOCAL.tts;
  })().catch((e) => {
    console.warn("on-device voice failed", e);
    LOCAL.loading = null;
    localNote().textContent = "This browser couldn't load the voice. The server's voice is used instead.";
    save({ local: false }); paintSettings();
  });
  return LOCAL.loading;
}
const cueId = (c) => `${c.kind || "ad"}-${c.start_ms}`;
const useLocal = () => S.local && LOCAL.tts && (W.lang || S.lang) === "en";
// voice the next lines ahead of time, one at a time
async function prepareLocal(tMs) {
  if (!useLocal() || LOCAL.busy) return;
  const next = W.cues.find((c) => c.start_ms > tMs - 500 && c.start_ms < tMs + 25000 && !LOCAL.clips.has(cueId(c)) &&
                                  (c.kind !== "dub" || S.dub));
  if (!next) return;
  LOCAL.busy = true;
  try {
    const audio = await LOCAL.tts.generate(next.text, { voice: next.kind === "dub" ? "bf_emma" : "am_michael" });
    const ms = (audio.audio.length / audio.sampling_rate) * 1000;
    LOCAL.clips.set(cueId(next), { url: URL.createObjectURL(audio.toBlob()), ms });
  } catch (e) { console.warn(e); LOCAL.clips.set(cueId(next), null); } finally { LOCAL.busy = false; }
}
// the clip to play for a line, and how fast so it fits its pause
function clipFor(c) {
  const mine = useLocal() && LOCAL.clips.get(cueId(c));
  if (!mine) return { url: c.audio_url, rate: S.rate };
  const room = c.gap ? c.gap[1] - c.gap[0] + (c.kind === "dub" ? 300 : -150) : mine.ms;
  return { url: mine.url, rate: Math.min(1.6, Math.max(S.rate, mine.ms / Math.max(600, room))) };
}

// ---------- views ----------
function show(view) {
  for (const id of ["home", "watch"]) $("#" + id).hidden = id !== view;
}
function route() {
  if (!S.welcomed) { $("#app").hidden = true; $("#welcome").hidden = false; step = 0; renderStep(); return; }
  $("#welcome").hidden = true;
  $("#app").hidden = false;
  const m = location.hash.match(/^#\/watch\/([\w-]{11})/);
  if (m) openVideo(m[1]);
  else { closeVideo(); show("home"); document.title = "Enjoyable Lite"; setTimeout(() => $("#h-title").focus(), 0); }
}
window.addEventListener("hashchange", route);

// ---------- welcome ----------
const ICONS = {
  globe: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/></svg>',
  ear: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8.5a6 6 0 1 1 12 0c0 3.5-3 4.5-3 7.5a3 3 0 0 1-6 0"/><path d="M9.5 9a2.5 2.5 0 0 1 5 0c0 1.5-1.5 2-1.5 3.5"/></svg>',
  eye: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>',
};
const STEPS = [
  { title: "How do you watch videos?", key: "look", choices: [
    { v: "listen", icon: "ear", label: "I listen", sub: "The app speaks to you" },
    { v: "see", icon: "eye", label: "I watch the screen", sub: "Large text, descriptions shown on screen" }] },
  { title: "Description language", key: "lang", choices: null },
  { title: "One shortcut to remember", final: true },
];
const TILE = { en: "EN", ms: "BM", zh: "中", id: "ID", ja: "日", ko: "한" };
let step = 0, langMode = false;

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v; else if (k === "html") e.innerHTML = v; else if (k.startsWith("on")) e[k] = v;
    else e.setAttribute(k, v);
  }
  for (const k of kids) if (k != null) e.append(k);
  return e;
}

function languageChoices() {
  const sys = systemLanguage();
  const picks = [...new Set([sys, "en", "ms", "zh"])].filter(langOf).slice(0, 4);
  if (S.lang && !picks.includes(S.lang) && langOf(S.lang)) picks[picks.length - 1] = S.lang;
  const out = picks.map((code) => {
    const [, en, native] = langOf(code);
    const sub = [code === sys ? "Your system's language" : "", native !== en ? en : ""].filter(Boolean).join(" · ");
    return { v: code, text: TILE[code] || code.toUpperCase(), label: native, sub, lang: code, speakAs: en };
  });
  out.push({ v: "__more", icon: "globe", label: "More languages", sub: `${LANGS.length} in all`, speakAs: "more languages" });
  return out;
}

function renderLangList(speak) {
  const box = $("#w-content");
  box.replaceChildren(
    el("label", { class: "sr", for: "lang-q" }, "Search languages"),
    el("input", { class: "lang-search", id: "lang-q", type: "search", placeholder: "Search languages", autocomplete: "off", spellcheck: "false" }),
    el("div", { class: "lang-list", role: "listbox", "aria-label": "Languages", id: "lang-list" }));
  const q = $("#lang-q"), list = $("#lang-list");
  const paint = () => {
    const t = q.value.trim().toLowerCase();
    const hits = LANGS.filter(([c, en, nat]) => !t || en.toLowerCase().includes(t) || nat.toLowerCase().includes(t) || c === t);
    list.replaceChildren(...hits.map(([code, en, native]) => el("button", { class: "lang-opt", role: "option", lang: code,
      "aria-selected": String(S.lang === code), onclick: () => { langMode = false; choose("lang", code); } },
      el("b", {}, native), el("span", {}, native !== en ? en : " "))));
    if (!hits.length) list.replaceChildren(el("p", { class: "lang-empty" }, "No language matches."));
  };
  q.addEventListener("input", paint);
  q.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); list.querySelector(".lang-opt")?.focus(); }
    if (e.key === "Enter") { e.preventDefault(); list.querySelector(".lang-opt")?.click(); }
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); langMode = false; renderStep(); }
  });
  list.addEventListener("keydown", (e) => {
    const opts = $$(".lang-opt", list), i = opts.indexOf(document.activeElement);
    const move = { ArrowDown: 2, ArrowUp: -2, ArrowRight: 1, ArrowLeft: -1 }[e.key];
    if (move && i >= 0) { e.preventDefault(); const n = i + move; if (n < 0) q.focus(); else opts[Math.min(opts.length - 1, n)].focus(); }
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); langMode = false; renderStep(); }
  });
  paint();
  q.focus();
  if (speak) say("More languages. Type a language name, then press Enter.");
}

function renderStep(speak = true) {
  const st = STEPS[step];
  if (st.key === "lang") st.choices = languageChoices();
  $("#w-step").textContent = `Step ${step + 1} of ${STEPS.length}`;
  $("#w-title").textContent = st.title;
  $$("#w-dots i").forEach((d, i) => d.classList.toggle("on", i === step));
  $("#w-back").style.visibility = step ? "visible" : "hidden";
  const box = $("#w-content");
  if (st.key === "lang" && langMode) return renderLangList(speak);
  if (st.final) {
    box.innerHTML = `
      <div class="keys" aria-hidden="true"><kbd>Ctrl</kbd><span class="plus">+</span><kbd>Shift</kbd><span class="plus">+</span><kbd>D</kbd></div>
      <p class="lead">Turns descriptions on and off.</p>
      <p class="hint">Press <kbd>F1</kbd> any time to hear every shortcut.</p>
      <button class="btn primary start" id="w-start">Start</button>`;
    $("#w-start").onclick = finishWelcome;
    $("#w-start").focus();
    if (speak) say("One shortcut to remember: Control Shift D turns descriptions on and off. Press F1 any time to hear every shortcut. Press Enter to start.");
    return;
  }
  const group = el("div", { class: "choices", role: "radiogroup", "aria-labelledby": "w-title" });
  st.choices.forEach((c, i) => {
    const b = el("button", { class: "choice", role: "radio", "aria-checked": String(S[st.key] === c.v) },
      el("span", { class: "ico" + (c.text ? " text" : ""), "aria-hidden": "true", html: c.icon ? ICONS[c.icon] : "" }),
      el("span", {}, el("b", {}, c.label), c.sub ? el("span", { class: "sub" }, c.sub) : null),
      el("kbd", { "aria-hidden": "true" }, String(i + 1)));
    if (c.text) b.firstChild.textContent = c.text;
    if (c.lang) b.lang = c.lang;
    b.onclick = () => (c.v === "__more" ? ((langMode = true), renderStep()) : choose(st.key, c.v));
    group.append(b);
  });
  box.replaceChildren(group);
  (group.querySelector('[aria-checked="true"]') || group.firstChild).focus();
  if (speak) {
    const opts = st.choices.map((c, i) => `Press ${i + 1} for ${c.speakAs || c.label}.`).join(" ");
    say((step === 0 ? "Welcome to Enjoyable Lite. " : "") + st.title + ". " + opts, { force: step === 0 });
  }
}
function choose(key, v) {
  const patch = { [key]: v };
  if (key === "look") Object.assign(patch, { speak: v === "listen", captions: v === "see" });
  save(patch);
  if (key === "look" && v === "see") hush();
  step = Math.min(step + 1, STEPS.length - 1);
  renderStep();
}
function finishWelcome() { save({ welcomed: true }); hush(); route(); }
$("#w-back").onclick = () => { if (langMode) langMode = false; else step = Math.max(0, step - 1); renderStep(); };
$("#welcome").addEventListener("keydown", (e) => {
  const st = STEPS[step];
  if (langMode) return;
  const n = parseInt(e.key, 10);
  if (!st.final && n >= 1 && n <= st.choices.length) {
    e.preventDefault();
    const c = st.choices[n - 1];
    if (c.v === "__more") { langMode = true; renderStep(); } else choose(st.key, c.v);
  }
  if (["ArrowDown", "ArrowUp"].includes(e.key) && !st.final) {
    const items = $$(".choice"), i = items.indexOf(document.activeElement);
    if (i >= 0) { e.preventDefault(); items[(i + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length].focus(); }
  }
  if (e.key === "Backspace" && step) { e.preventDefault(); step--; renderStep(); }
});

// ---------- home ----------
function videoIdFrom(text) {
  const s = text.trim();
  if (/^[\w-]{11}$/.test(s)) return s;
  try {
    const u = new URL(s.startsWith("http") ? s : "https://" + s);
    const host = u.hostname.replace(/^(www|m|music)\./, "");
    if (host === "youtu.be") return u.pathname.slice(1, 12);
    if (host === "youtube.com" || host === "youtube-nocookie.com") {
      if (u.searchParams.get("v")) return u.searchParams.get("v").slice(0, 11);
      const m = u.pathname.match(/^\/(shorts|embed|live|v)\/([\w-]{11})/);
      if (m) return m[2];
    }
  } catch { /* not a link */ }
  return null;
}
$("#go").addEventListener("submit", (e) => {
  e.preventDefault();
  const id = videoIdFrom($("#url").value);
  const err = $("#url-err");
  if (!id) {
    err.hidden = false;
    err.textContent = "That doesn't look like a YouTube link. Try one like youtube.com/watch?v=…";
    $("#url").focus();
    return;
  }
  err.hidden = true;
  location.hash = "#/watch/" + id;
});
function renderShelf() {
  $("#shelf").replaceChildren(...DEMOS.map((d) => el("li", {}, el("button", { class: "card", onclick: () => { location.hash = "#/watch/" + d.id; } },
    el("span", { class: "thumb" }, el("img", { src: `https://i.ytimg.com/vi/${d.id}/maxresdefault.jpg`, alt: "", loading: "lazy",
      onerror: (e) => { e.target.onerror = null; e.target.src = `https://i.ytimg.com/vi/${d.id}/mqdefault.jpg`; } }),
      el("span", { class: "badge", "aria-hidden": "true" }, d.len || "")),
    el("b", {}, d.title), el("span", { class: "sub" }, d.sub || "")))));
  $("#try").hidden = !DEMOS.length;
}

// ---------- the YouTube player ----------
let player = null, playerReady = null;
function loadYouTube() {
  if (playerReady) return playerReady;
  playerReady = new Promise((resolve) => {
    window.onYouTubeIframeAPIReady = resolve;
    document.head.append(el("script", { src: "https://www.youtube.com/iframe_api" }));
  });
  return playerReady;
}

// ---------- watching ----------
const W = { id: null, key: null, cues: [], played: new Set(), video: {}, unsub: [], poll: null, beat: null, tick: null,
            lastT: 0, gated: true, waiting: false, wantPlay: false, last: null };
const fmt = (s) => { s = Math.max(0, Math.floor(s || 0)); const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, x = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${x}` : `${m}:${x}`; };
const now = () => (player?.getCurrentTime ? player.getCurrentTime() : 0);
const dur = () => (player?.getDuration ? player.getDuration() : 0);
const playing = () => player?.getPlayerState?.() === 1;

function setStatus(text) { $("#status").textContent = text; }
// "Ready" means ready to play through: the first two parts (45 s of video) are prepared, and each next part is ready
// before the viewer reaches it. Earlier, playback would stop to wait for the second part.
const PLAY_THROUGH_MS = 40000;
function covered(tMs) {
  const p = W.video.prepared || [];
  return p.some(([a, b]) => a <= tMs && tMs < b) || W.video.status === "ready";
}

async function openVideo(id) {
  if (W.id === id) return;
  closeVideo();
  show("watch");
  W.id = id;
  W.lang = S.lang || systemLanguage();
  W.gated = true;
  $("#v-title").textContent = "";
  $("#cap").textContent = "";
  setGate("Getting the descriptions ready", "A new video takes under a minute, then it never pauses.");
  setStatus("");
  document.title = "Enjoyable Lite";
  await loadYouTube();
  if (W.id !== id) return;
  $("#player").replaceWith(el("div", { id: "player" }));
  player = new YT.Player("player", {
    videoId: id, host: "https://www.youtube-nocookie.com",
    playerVars: { controls: 0, disablekb: 1, rel: 0, playsinline: 1, iv_load_policy: 3, fs: 0, modestbranding: 1,
                   cc_load_policy: 0 }, // our own captions; YouTube's would cover the picture twice
    events: { onReady: () => onPlayerReady(id), onStateChange: onState, onError: (e) => onPlayerError(e.data) },
  });
}
// YouTube's own captions would cover the picture under ours. They load with playback and come back from a viewer's
// saved YouTube preference, so they are switched off again whenever playback starts.
function noCaptions() {
  try { player.setOption("captions", "track", {}); player.unloadModule("captions"); player.unloadModule("cc"); }
  catch { /* no captions module */ }
}
function onPlayerReady(id) {
  if (W.id !== id) return;
  noCaptions();
  volNow = null;
  duck("voice", false, true);  // the viewer's saved video volume from the start, at once
  const iframe = $("#stage iframe");
  if (iframe) { iframe.setAttribute("tabindex", "-1"); iframe.setAttribute("aria-hidden", "true"); iframe.title = "Video"; }
  const title = player.getVideoData?.().title || "";
  $("#v-title").textContent = title;
  document.title = (title ? title + " · " : "") + "Enjoyable Lite";
  start(id);
  $("#v-title").focus();
}
function onPlayerError(code) {
  const msg = code === 101 || code === 150 ? "The owner of this video doesn't allow it to play on other websites. Try another video."
    : "This video can't be played. Check the link, or try another video.";
  setGate("Can't play this video", msg, false);
  say(msg);
}

async function start(id) {
  setStatus("");
  let r;
  try {
    r = await fetch(API + "/api/watch", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ video_id: id, lang: W.lang, t: 0, duration: dur() }) }).then((x) => x.json());
  } catch {
    setGate("Can't reach Enjoyable", "Check your internet connection, then reload the page.", false);
    return;
  }
  if (W.id !== id) return;
  W.key = r.key;
  listen(r.key);
  W.beat = setInterval(heartbeat, 12000);
  W.tick = setInterval(tick, 150);
  say(`${player.getVideoData?.().title || "Video"}. Getting the descriptions ready.`);
}

function heartbeat() {
  if (!W.key) return;
  fetch(API + "/api/heartbeat", { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ key: W.key, t: now(), duration: dur() }) }).catch(() => {});
}

// live data: Firestore when configured, otherwise ask the server every few seconds
async function listen(key) {
  if (CFG.firebase) {
    try {
      const [{ initializeApp }, fs] = await Promise.all([
        import("https://www.gstatic.com/firebasejs/12.0.0/firebase-app.js"),
        import("https://www.gstatic.com/firebasejs/12.0.0/firebase-firestore.js")]);
      const app = window.__fb || (window.__fb = initializeApp(CFG.firebase));
      const db = fs.getFirestore(app);
      W.unsub.push(fs.onSnapshot(fs.doc(db, "videos", key), (snap) => {
        const v = snap.data() || {};
        v.prepared = (v.prepared || []).map((p) => [p.a, p.b]);
        onVideo(v);
      }));
      W.unsub.push(fs.onSnapshot(fs.query(fs.collection(db, "videos", key, "cues"), fs.orderBy("start_ms")), (snap) => {
        onCues(snap.docs.map((d) => d.data()));
      }));
      return;
    } catch (e) { console.warn("Firestore unavailable, polling", e); }
  }
  const pull = async () => {
    try {
      const r = await fetch(`${API}/api/cues?key=${encodeURIComponent(key)}`).then((x) => x.json());
      if (W.key !== key) return;
      onVideo(r.video || {});
      onCues(r.cues || []);
    } catch { /* next time */ }
  };
  pull();
  W.poll = setInterval(pull, 2500);
}
function onVideo(v) {
  if (v.dubbed && !W.video.dubbed && S.dub) say("This video is in another language. Voice-over is on.");
  W.video = v;
  paintTranslation();
  if (v.status === "paused" || v.status === "error") {
    const msg = v.status === "paused" ? "Today's limit for new descriptions is reached. Videos already described still work."
      : "Something went wrong while describing this video. Try again in a minute.";
    setStatus(msg);
    if (W.gated) { setGate("Descriptions paused", msg, false); W.gated = false; setTimeout(() => setGate(null), 4000); }
  }
}
function onCues(list) {
  W.cues = list.sort((a, b) => a.start_ms - b.start_ms);
}

function setGate(title, sub, spinner = true) {
  $("#stage .gate")?.remove();
  if (!title) return;
  $("#stage").append(el("div", { class: "gate", "aria-hidden": "true" },
    el("div", {}, spinner ? el("div", { class: "spin" }) : null, el("b", {}, title), el("span", {}, sub || ""))));
  setStatus(title + ". " + (sub || ""));
  if (!spinner || W.waiting) announce(title + ". " + (sub || "")); // "getting ready" is already spoken
}

function tick() {
  if (!player?.getCurrentTime) return;
  const t = now(), tMs = t * 1000, d = dur();
  $("#time").textContent = `${fmt(t)} / ${fmt(d)}`;
  if (d && document.activeElement !== $("#seek")) $("#seek").value = String(Math.round((t / d) * 1000));
  $("#seek").setAttribute("aria-valuetext", `${fmt(t)} of ${fmt(d)}`);

  // enough is prepared to play through: let the viewer start
  if (W.gated && (covered(tMs + PLAY_THROUGH_MS) || (d && covered(d * 1000 - 1)))) { // it can now play without a pause
    W.gated = false;
    setGate(null);
    setStatus("Ready.");
    say("Ready. Press Space to play.");
    $("#play").focus();
  }
  // playing past what is prepared: wait for the next part instead of missing descriptions
  if (S.on && !W.gated && playing() && W.video.status !== "paused" && W.video.status !== "error" && d && tMs < d * 1000 - 2000 && !covered(tMs + 2500)) {
    W.waiting = true;
    player.pauseVideo();
    setGate("Preparing the next part", "It continues by itself.");
  } else if (W.waiting && covered(tMs + 15000)) {
    W.waiting = false;
    setGate(null);
    player.playVideo();
  }
  // a jump: forget what was played after the new position, tell the server
  if (Math.abs(t - W.lastT) > 1.5) {
    for (const s of [...W.played]) if (s > tMs - 500) W.played.delete(s);
    if (!voice.paused || !dubVoice.paused) hush();
    heartbeat();
  }
  W.lastT = t;
  prepareLocal(tMs);
  if (!S.on || !playing()) return;
  for (const c of W.cues) {
    if (c.start_ms > tMs) break;
    if (W.played.has(c.start_ms)) continue;
    W.played.add(c.start_ms);
    if (tMs - c.start_ms > 700) continue; // too late for this one
    if (c.kind === "dub") { if (S.dub) speakDub(c); continue; }
    if (!voice.paused && W.last?.kind === "ad" && W.lastEnd > c.start_ms + 300) continue; // still speaking
    speakCue(c);
  }
}
function showCap(text) {
  if (!(S.captions || S.look === "see")) return;
  $("#cap").textContent = text;
}
function clearCap(text) { if ($("#cap").textContent === text) $("#cap").textContent = ""; }
function speakCue(c) {
  W.last = c;
  W.lastEnd = c.end_ms || c.start_ms + 4000;
  showCap(c.text);
  const { url, rate } = clipFor(c);
  playVoice(url, { rate, onend: () => clearCap(c.text) });
  setStatus(c.text);
}
function speakDub(c) {
  showCap(c.text);
  const { url, rate } = clipFor(c);
  playVoice(url, { rate, el: dubVoice, onend: () => clearCap(c.text) });
}

function closeVideo() {
  hush();
  for (const u of W.unsub) try { u(); } catch { /* ignore */ }
  clearInterval(W.poll); clearInterval(W.beat); clearInterval(W.tick);
  Object.assign(W, { id: null, key: null, cues: [], played: new Set(), video: {}, unsub: [], poll: null, beat: null, tick: null,
    lastT: 0, gated: true, waiting: false, last: null });
  if (player?.destroy) { try { player.destroy(); } catch { /* ignore */ } }
  player = null;
}

function onState(e) {
  const isPlaying = e.data === 1;
  if (isPlaying || e.data === 3) noCaptions();
  $("#i-play").style.display = isPlaying ? "none" : "";
  $("#i-pause").style.display = isPlaying ? "" : "none";
  $("#play").setAttribute("aria-label", isPlaying ? "Pause" : "Play");
  if (e.data === 2 && !W.waiting) hush();
  if (e.data === 0) { setStatus("The end."); say("The end."); }
}

// ---------- the controls ----------
function togglePlay() {
  if (!player?.getPlayerState) return;
  if (W.gated) { say("Still getting ready. One moment."); return; }
  if (playing() || W.waiting) { W.waiting = false; setGate(null); player.pauseVideo(); } else player.playVideo();
}
function skip(s) {
  if (!player?.seekTo) return;
  const t = Math.max(0, Math.min(dur() - 1, now() + s));
  player.seekTo(t, true);
  announce(fmt(t));
}
function toggleOn() {
  save({ on: !S.on });
  paintOnOff();
  if (!S.on) { hush(); $("#cap").textContent = ""; }
  say(S.on ? "Descriptions on" : "Descriptions off", { force: true });
}
function paintOnOff() {
  $("#onoff").setAttribute("aria-checked", String(S.on));
  $("#onoff-state").textContent = S.on ? "On" : "Off";
}
function repeat() {
  if (!W.last) { say("Nothing to repeat yet."); return; }
  if (S.captions || S.look === "see") $("#cap").textContent = W.last.text;
  playVoice(W.last.kind === "scene" ? W.last.audio_url : clipFor(W.last).url);
  announce(W.last.text);
}
let sceneBusy = false;
async function scene() {
  if (!W.id || sceneBusy) return;
  sceneBusy = true;
  const wasPlaying = playing();
  if (wasPlaying) player.pauseVideo();
  setStatus("Looking at the screen…");
  announce("Looking at the screen.");
  try {
    const r = await fetch(API + "/api/scene", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ video_id: W.id, t: now(), lang: W.lang }) }).then((x) => x.json());
    W.last = { text: r.text, audio_url: r.audio_url, kind: "scene" };
    if (S.captions || S.look === "see") $("#cap").textContent = r.text;
    setStatus(r.text);
    announce(r.text);
    playVoice(r.audio_url, { duck: false, onend: () => { $("#cap").textContent = ""; if (wasPlaying) player.playVideo(); } });
  } catch {
    say("Couldn't look at the screen right now.");
    if (wasPlaying) player.playVideo();
  } finally { sceneBusy = false; }
}
function setVoiceVolume(v) {
  save({ vol: Math.round(Math.max(0.2, Math.min(1, v)) * 100) / 100 });
  voice.volume = S.vol;
  paintSettings();
}
function setDubVolume(v) {
  save({ dvol: Math.round(Math.max(0.2, Math.min(1, v)) * 100) / 100 });
  dubVoice.volume = S.dvol;
  paintSettings();
}
function setVideoVolume(v, mute = false) {
  save({ vvol: Math.round(Math.max(0, Math.min(1, v)) * 100) / 100, mute });
  duck("voice", ducking.voice, true);  // the viewer's own change: at once, with the current dip
  paintSettings();
}
function nudgeVolume(d) {
  setVoiceVolume(S.vol + d);
  say(`Description volume ${Math.round(S.vol * 100)} percent`, { force: true });
}
function nudgeVideo(d) {
  setVideoVolume(S.vvol + d);
  announce(`Video volume ${Math.round(S.vvol * 100)} percent`);
}
function toggleDip(key, what) {
  save({ [key]: !S[key] });
  duck("voice", ducking.voice);
  paintSettings();
  announce(S[key] ? `${what}: on` : `${what}: off`);
}
function toggleMute() {
  setVideoVolume(S.vvol, !S.mute);
  announce(S.mute ? "Video muted" : "Video sound on");
}

$("#play").onclick = togglePlay;
$("#veil").onclick = togglePlay;
$("#back10").onclick = () => skip(-10);
$("#fwd10").onclick = () => skip(10);
$("#onoff").onclick = toggleOn;
$("#repeat").onclick = repeat;
$("#scene").onclick = scene;
$("#seek").addEventListener("change", () => { if (dur()) player.seekTo((Number($("#seek").value) / 1000) * dur(), true); });
window.__seek = (t) => player?.seekTo(t, true); // for the screenshot script

// ---------- settings ----------
let lastFocus = null;
function openOverlay(id) {
  lastFocus = document.activeElement;
  for (const x of ["settings", "help"]) $("#" + x).hidden = x !== id;
  if (id === "settings") paintSettings();
  if (id === "help") paintHelp();
  $(id === "settings" ? "#s-title" : "#k-title").focus();
  if (id === "help") say("Keyboard shortcuts. " + SHORTCUTS.map(([k, w]) => `${spoken(k)}: ${w}.`).join(" ") + " Press Escape to close.");
}
function closeOverlay() {
  const open = $$(".overlay").find((o) => !o.hidden);
  if (!open) return false;
  open.hidden = true;
  hush();
  (lastFocus && document.contains(lastFocus) ? lastFocus : $("#main")).focus();
  return true;
}
$$("[data-close]").forEach((b) => (b.onclick = closeOverlay));
$("#open-settings").onclick = () => openOverlay("settings");
$("#open-help").onclick = () => openOverlay("help");
$("#again").onclick = () => { closeOverlay(); save({ welcomed: false }); route(); };

function paintSettings() {
  $$(".seg").forEach((g) => $$("button", g).forEach((b) => b.setAttribute("aria-checked", String(String(S[g.dataset.key]) === b.dataset.v))));
  $$(".switch").forEach((b) => b.setAttribute("aria-checked", String(!!S[b.dataset.key])));
  $("#r-rate").value = S.rate; $("#v-rate").textContent = `${Number(S.rate).toFixed(1)}×`;
  $("#r-vol").value = S.vol; $("#v-vol").textContent = `${Math.round(S.vol * 100)}%`;
  $("#r-rate").setAttribute("aria-valuetext", `${Number(S.rate).toFixed(1)} times`);
  $("#r-vol").setAttribute("aria-valuetext", `${Math.round(S.vol * 100)} percent`);
  const vv = S.mute ? 0 : S.vvol;
  $("#r-vvol").value = S.vvol; $("#v-vvol").textContent = `${Math.round(S.vvol * 100)}%`;
  $("#r-vvol").setAttribute("aria-valuetext", `${Math.round(S.vvol * 100)} percent`);
  $("#m-video").value = vv; $("#mv-video").textContent = S.mute ? "Muted" : `${Math.round(vv * 100)}%`;
  $("#m-video").setAttribute("aria-valuetext", S.mute ? "muted" : `${Math.round(vv * 100)} percent`);
  $("#m-voice").value = S.vol; $("#mv-voice").textContent = `${Math.round(S.vol * 100)}%`;
  $("#m-voice").setAttribute("aria-valuetext", `${Math.round(S.vol * 100)} percent`);
  $("#mute").setAttribute("aria-pressed", String(!!S.mute));
  $("#mute-label").textContent = S.mute ? "Muted" : "Mute";
  $(".mixer").dataset.muted = String(!!S.mute);
  $("#m-dub").value = S.dvol; $("#mv-dub").textContent = `${Math.round(S.dvol * 100)}%`;
  $("#m-dub").setAttribute("aria-valuetext", `${Math.round(S.dvol * 100)} percent`);
  $("#r-dvol").value = S.dvol; $("#v-dvol").textContent = `${Math.round(S.dvol * 100)}%`;
  $("#r-dvol").setAttribute("aria-valuetext", `${Math.round(S.dvol * 100)} percent`);
  for (const [id, key] of [["voice", "dipVoice"], ["dub", "dipDub"]]) {
    $(`#dip-${id}`).setAttribute("aria-checked", String(!!S[key]));
    $(`#dip-${id}-state`).textContent = S[key] ? "On" : "Off";
  }
  paintTranslation();
  $("#sel-lang").value = S.lang || systemLanguage();
}
$$(".seg").forEach((g) => {
  $$("button", g).forEach((b) => (b.onclick = () => {
    const patch = { [g.dataset.key]: b.dataset.v };
    if (g.dataset.key === "look") Object.assign(patch, { speak: b.dataset.v === "listen", captions: b.dataset.v === "see" });
    save(patch); paintSettings(); announce(b.textContent);
  }));
  g.addEventListener("keydown", (e) => { // arrow keys move within a radio group
    const bs = $$("button", g), i = bs.indexOf(document.activeElement);
    const d = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
    if (d && i >= 0) { e.preventDefault(); const n = bs[(i + d + bs.length) % bs.length]; n.focus(); n.click(); }
  });
});
$$(".switch").forEach((b) => (b.onclick = () => {
  save({ [b.dataset.key]: !S[b.dataset.key] });
  paintSettings();
  if (b.dataset.key === "dub" && !S.dub) dubVoice.pause();
  if (b.dataset.key === "local") {
    if (S.local) { localNote().textContent = "Getting the voice…"; loadLocalVoice(); }
    else localNote().textContent = LOCAL_NOTE;
  }
}));
$("#r-rate").addEventListener("input", (e) => { save({ rate: Number(e.target.value) }); paintSettings(); });
$("#r-rate").addEventListener("change", () => say("This is how fast I speak.", { force: true }));
$("#r-vol").addEventListener("input", (e) => setVoiceVolume(Number(e.target.value)));
$("#m-voice").addEventListener("input", (e) => setVoiceVolume(Number(e.target.value)));
$("#r-vvol").addEventListener("input", (e) => setVideoVolume(Number(e.target.value)));
$("#m-video").addEventListener("input", (e) => setVideoVolume(Number(e.target.value)));
$("#mute").onclick = toggleMute;
$("#m-dub").addEventListener("input", (e) => setDubVolume(Number(e.target.value)));
$("#r-dvol").addEventListener("input", (e) => setDubVolume(Number(e.target.value)));
$("#dip-voice").onclick = () => toggleDip("dipVoice", "Turn original voices down during descriptions");
$("#dip-dub").onclick = () => toggleDip("dipDub", "Turn original voices down during translation");
// Translation: dialogue in other languages is voiced in the chosen language. The service prepares descriptions and
// translation together in one language, so choosing a language here also sets the description language.
function paintTranslation() {
  const lang = S.lang || systemLanguage();
  const name = (langOf(lang) || [lang, lang])[1];
  $("#sel-trans").value = S.dub ? lang : "off";
  $("#col-dub").dataset.off = String(!S.dub);
  const v = W.id ? W.video || {} : {};
  const started = (v.prepared || []).length > 0;
  $("#trans-note").textContent = !S.dub ? "Off: other languages play as they are."
    : v.dubbed ? `Other languages are voiced in ${name}.`
    : started ? `None: this video is already in ${name}.`
    : `Into ${name}. Descriptions use the same language.`;
}
$("#sel-trans").addEventListener("change", (e) => {
  if (e.target.value === "off") { save({ dub: false }); dubVoice.pause(); duck("dub", false); paintTranslation(); return; }
  save({ dub: true });
  if (e.target.value !== (S.lang || systemLanguage())) {  // a new language: prepared again, like the Settings choice
    $("#sel-lang").value = e.target.value;
    $("#sel-lang").dispatchEvent(new Event("change"));
  }
  paintTranslation();
});
$("#sel-lang").addEventListener("change", (e) => {
  save({ lang: e.target.value });
  if (W.id) { const id = W.id; closeVideo(); openVideo(id); } // describe again in the new language
});

// ---------- shortcuts ----------
const SHORTCUTS = [
  [["Space"], "Play or pause"], [["J"], "Back 10 seconds"], [["L"], "Forward 10 seconds"],
  [["Ctrl", "Shift", "D"], "Descriptions on or off"], [["Ctrl", "Shift", "R"], "Repeat the last description"],
  [["Ctrl", "Shift", "S"], "Describe what is on screen now"], [["↑"], "Video louder"], [["↓"], "Video softer"],
  [["M"], "Mute or unmute the video"], [["Ctrl", "Shift", "↑"], "Descriptions louder"],
  [["Ctrl", "Shift", "↓"], "Descriptions softer"], [["Ctrl", ","], "Settings"], [["F1"], "This list"],
];
const SPOKEN = { "↑": "up arrow", "↓": "down arrow", ",": "comma", Ctrl: "Control" };
const spoken = (keys) => keys.map((k) => SPOKEN[k] || k).join(" ");
function paintHelp() {
  $("#rows").replaceChildren(...SHORTCUTS.flatMap(([keys, what]) => [
    el("dt", {}, ...keys.flatMap((k, i) => (i ? [" + ", el("kbd", {}, k)] : [el("kbd", {}, k)]))), el("dd", {}, what)]));
}
document.addEventListener("keydown", (e) => {
  const k = e.key.toLowerCase();
  const typing = /^(input|select|textarea)$/i.test(e.target.tagName) && e.target.type !== "range";
  if (e.key === "Escape" && closeOverlay()) { e.preventDefault(); return; }
  if (e.key === "F1") { e.preventDefault(); openOverlay("help"); return; }
  if (e.ctrlKey && k === ",") { e.preventDefault(); openOverlay("settings"); return; }
  if (!$("#welcome").hidden || $$(".overlay").some((o) => !o.hidden)) return;
  if (e.ctrlKey && e.shiftKey) {
    const act = { d: toggleOn, r: repeat, s: scene, arrowup: () => nudgeVolume(0.1), arrowdown: () => nudgeVolume(-0.1) }[k];
    if (act && W.id) { e.preventDefault(); act(); }
    else if (k === "d") { e.preventDefault(); toggleOn(); }
    return;
  }
  if (typing || e.ctrlKey || e.altKey || e.metaKey || $("#watch").hidden) return;
  if (k === " " && e.target.tagName === "BUTTON") return; // Space presses the focused button
  if (k === " " || k === "k") { e.preventDefault(); togglePlay(); }
  if (k === "j") skip(-10);
  if (k === "l") skip(10);
  if (k === "m") toggleMute();
  if ((k === "arrowup" || k === "arrowdown") && e.target.type !== "range") { e.preventDefault(); nudgeVideo(k === "arrowup" ? 0.1 : -0.1); }
});

// ---------- start ----------
(async function init() {
  applyLook();
  try { LANGS = await fetch("languages.json").then((r) => r.json()); } catch { LANGS = [["en", "English", "English"]]; }
  $("#sel-lang").replaceChildren(...LANGS.map(([c, en, nat]) => el("option", { value: c }, nat === en ? en : `${nat} · ${en}`)));
  $("#sel-trans").replaceChildren(el("option", { value: "off" }, "Off"),
    ...LANGS.map(([c, en, nat]) => el("option", { value: c }, nat === en ? en : `${nat} · ${en}`)));
  if (!S.lang) save({ lang: systemLanguage() });
  paintOnOff();
  paintSettings();  // the volume row on the player shows the saved levels
  renderShelf();
  fetch(API + "/api/health").catch(() => {}); // wake the server while the viewer picks a video
  if (S.local) loadLocalVoice(); // already downloaded once: from the browser's cache
  route();
})();

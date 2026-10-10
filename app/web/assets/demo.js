// AI receptionist demo: text or voice (Web Speech API) conversation with /demo/chat.
(() => {
  const $ = (id) => document.getElementById(id);
  const log = $("log"), statusEl = $("status"), outcomeEl = $("outcome"), input = $("text");
  const micBtn = $("mic"), sendBtn = $("send"), speakToggle = $("speak");

  const GREETINGS = {
    hi: "नमस्ते, Aangan Studio में कॉल करने के लिए धन्यवाद। मैं स्टूडियो का AI असिस्टेंट हूँ — यह बातचीत रिकॉर्ड होती है ताकि हमारे डिज़ाइनरों के पास आपकी जानकारी रहे। बताइए, मैं आपकी कैसे मदद करूँ?",
  };
  const TRIES = [
    ["I have a 2BHK in Wakad and want a full redesign by March.", "en"],
    ["मुझे कोथरूड में अपने 3BHK फ्लैट का पूरा इंटीरियर करवाना है।", "hi"],
    ["Roughly how much would it cost per square foot?", "en"],
    ["I'm opening a café in Baner and need interiors.", "en"],
  ];
  const OUTCOME_TEXT = {
    QUALIFIED: "The project fits the studio's criteria. On a real call the designer would now get the full brief on Telegram.",
    DECLINE_SCOPE: "Outside the kind of work the studio takes on. Declined politely, with an invitation to come back.",
    DECLINE_AREA: "Outside Pune and PCMC, where the studio has its vendor network. Declined politely.",
    DECLINE_TIMELINE: "The deadline is too close for design plus execution. A later start was offered.",
    BUDGET_MISALIGNED: "The budget mentioned is well below what this scope needs. Said honestly, no figures given.",
    HUMAN_REVIEW: "Something stayed unclear, so a person from the studio would call back within a working day.",
    ESCALATE_URGENT: "An existing client with a problem. A senior person would call back within 15 minutes.",
    NOT_APPLICABLE: "Not a project enquiry. A message was taken.",
  };

  let state = { sessionId: null, lang: "en", busy: false, enabled: false, greeting: "", callId: null, voiceTurn: false };
  // Only the language choice survives a reload; every page load starts a fresh conversation.
  try { const lang = localStorage.getItem("aangan-demo-lang"); if (lang === "en" || lang === "hi") state.lang = lang; } catch {}
  const persist = () => { try { localStorage.setItem("aangan-demo-lang", state.lang); } catch {} };

  // ---------- rendering ----------
  function add(kind, text) {
    const node = el("div", { class: `msg ${kind}`, text });
    log.append(node);
    log.scrollTop = log.scrollHeight;
    return node;
  }
  function typing(on) {
    $("typing")?.remove();
    if (on) { log.append(el("div", { class: "msg agent typing", id: "typing", "aria-label": "Receptionist is typing" }, el("i"), el("i"), el("i"))); log.scrollTop = log.scrollHeight; }
  }
  function setStatus(text) { statusEl.textContent = text; }
  function setBusy(b) {
    state.busy = b;
    input.disabled = b || !state.enabled;
    sendBtn.disabled = b || !state.enabled;
    micBtn.disabled = b || !state.enabled || !Recognition;
    document.querySelectorAll(".try").forEach((t) => (t.disabled = b || !state.enabled));
  }
  function showOutcome(outcome, flags, callId) {
    outcomeEl.replaceChildren();
    if (!outcome) return;
    const top = el("div", { class: "outcome-top" }, el("div", {}, outcomeBadge(outcome), ...(flags || []).map((f) => [" ", flagBadge(f)])));
    if (callId) top.append(el("a", { href: `/dashboard/calls/${callId}`, text: "See it on the dashboard" }));
    outcomeEl.append(top, el("p", { text: OUTCOME_TEXT[outcome] || "" }));
  }
  function greet() {
    log.replaceChildren();
    add("agent", state.lang === "hi" ? GREETINGS.hi : state.greeting);
  }

  // ---------- speech out ----------
  const synth = "speechSynthesis" in window ? window.speechSynthesis : null;
  function pickVoice(lang) {
    if (!synth) return null;
    const voices = synth.getVoices(), want = lang === "hi" ? "hi" : "en";
    return voices.find((v) => v.lang === (want === "hi" ? "hi-IN" : "en-IN"))
      || voices.find((v) => v.lang?.toLowerCase().startsWith(want)) || null;
  }
  function speak(text, then) {
    if (!synth || !speakToggle.checked) { then?.(); return; }
    synth.cancel();
    const u = new SpeechSynthesisUtterance(text);
    const voice = pickVoice(state.lang);
    if (voice) u.voice = voice;
    u.lang = voice?.lang || (state.lang === "hi" ? "hi-IN" : "en-IN");
    u.rate = 1.02;
    u.onstart = () => setStatus("Speaking…");
    u.onend = u.onerror = () => { setStatus("Ready"); then?.(); };
    synth.speak(u);
  }
  synth?.addEventListener?.("voiceschanged", () => {});

  // ---------- speech in ----------
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition || null;
  let rec = null, listening = false;
  function stopListening() { listening = false; micBtn.classList.remove("on"); micBtn.setAttribute("aria-label", "Speak"); try { rec?.stop(); } catch {} }
  function listen() {
    if (!Recognition || state.busy || !state.enabled) return;
    synth?.cancel();
    rec = new Recognition();
    rec.lang = state.lang === "hi" ? "hi-IN" : "en-IN";
    rec.interimResults = true;
    rec.maxAlternatives = 1;
    let finalText = "";
    rec.onresult = (e) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) finalText += r[0].transcript; else interim += r[0].transcript;
      }
      input.value = (finalText + interim).trim();
    };
    rec.onerror = (e) => {
      if (e.error === "not-allowed" || e.error === "service-not-allowed") add("note", "Microphone access was blocked. Allow it in the browser, or type instead.");
      else if (e.error !== "no-speech" && e.error !== "aborted") add("note", "Couldn't hear that clearly. Try again, or type.");
    };
    rec.onend = () => {
      const said = (finalText || input.value).trim();
      stopListening();
      if (said) send(said, true); else setStatus("Ready");
    };
    listening = true;
    micBtn.classList.add("on");
    micBtn.setAttribute("aria-label", "Stop listening");
    setStatus("Listening…");
    rec.start();
  }
  micBtn.addEventListener("click", () => (listening ? stopListening() : listen()));

  // ---------- conversation ----------
  async function send(text, viaVoice = false) {
    text = (text || "").trim();
    if (!text || state.busy || !state.enabled) return;
    state.voiceTurn = viaVoice;
    input.value = "";
    add("visitor", text);
    setBusy(true); typing(true); setStatus("Thinking…");
    try {
      const resp = await fetch(new URL("/demo/chat", location.origin), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: state.sessionId, message: text, language: state.lang }),
      });
      const data = await resp.json().catch(() => ({}));
      typing(false);
      if (!resp.ok) { add("note", data.error || `Something went wrong (${resp.status}).`); setStatus("Ready"); return; }
      state.sessionId = data.session_id; state.callId = data.call_id; persist();
      add("agent", data.reply);
      showOutcome(data.outcome, (data.flags || []).filter((f) => f !== "PRICING_ASKED"), data.call_id);
      if (data.turns_left <= 5) $("foot-note").textContent = `${data.turns_left} messages left in this conversation.`;
      setBusy(false);
      speak(data.reply, () => { if (state.voiceTurn && Recognition && data.turns_left > 0) listen(); });
    } catch {
      typing(false); add("note", "Couldn't reach the receptionist. Check your connection and try again."); setStatus("Ready");
    } finally {
      if (state.busy) setBusy(false);
    }
  }
  $("composer").addEventListener("submit", (e) => { e.preventDefault(); stopListening(); send(input.value); });

  function reset() {
    stopListening(); synth?.cancel();
    state.sessionId = null; state.callId = null; persist();
    outcomeEl.replaceChildren(); $("foot-note").textContent = "Demo bookings are not real appointments.";
    greet(); setStatus(state.enabled ? "Ready" : "Offline");
  }
  $("reset").addEventListener("click", reset);

  $("lang").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-lang]");
    if (!b || b.dataset.lang === state.lang) return;
    state.lang = b.dataset.lang; persist();
    $("lang").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    document.documentElement.lang = state.lang === "hi" ? "hi" : "en";
    if (!state.sessionId) greet();
  });

  const tries = $("tries");
  TRIES.forEach(([text, lang], i) => tries.append(el("button", {
    class: "try", type: "button",
    onclick: () => {
      if (lang !== state.lang) $("lang").querySelector(`[data-lang="${lang}"]`).click();
      send(text);
      if (window.matchMedia("(max-width: 900px)").matches) $("log").scrollIntoView({ behavior: "smooth", block: "center" });
    },
  }, el("span", { text }), el("small", { text: lang === "hi" ? "Hindi" : "English" }))));

  // ---------- start ----------
  revealOnScroll(); // show the page at once; don't wait for the server
  (async () => {
    $("lang").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.lang === state.lang)));
    if (!Recognition) micBtn.title = "Voice input works in Chrome, Edge and Safari. Type instead.";
    if (!synth) speakToggle.closest("label").hidden = true;
    try {
      const s = await getJSON("/demo/status");
      state.enabled = s.enabled; state.greeting = s.greeting;
      greet();
      if (!s.enabled) { add("note", "The live receptionist isn't switched on yet. The studio needs to add its Claude API key."); setStatus("Offline"); }
      else setStatus("Ready");
    } catch {
      state.greeting = "Hello, thank you for calling Aangan Studio."; greet();
      add("note", "Couldn't reach the receptionist right now. Refresh to try again."); setStatus("Offline");
    }
    setBusy(false);
    revealOnScroll();
  })();
})();

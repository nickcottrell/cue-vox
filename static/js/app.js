// ============================================
// CUE-VOX V2 - Full Implementation
// ============================================

// DOM Elements
const socket = io();
const drawerToggle = document.getElementById('drawerToggle');
const drawer = document.getElementById('drawer');
const conversation = document.getElementById('conversation');
const stateDot = document.querySelector('.state-dot__inner');
const drawerStatusDot = document.getElementById('drawerStatusDot');
const drawerStatusText = document.getElementById('drawerStatusText');
const canvasStatus = document.getElementById('canvasStatus');
const drawerTextInput = document.getElementById('drawerTextInput');
const drawerSendButton = document.getElementById('drawerSendButton');
const canvasStopBtn = document.getElementById('canvasStopBtn');
const drawerStopLink = document.getElementById('drawerStopLink');

// State
let mediaRecorder;
let audioChunks = [];
let isRecording = false;
let currentState = 'idle';
let hasPendingInput = false;
let lastMessageHash = null; // Prevent duplicate messages
let stateTimerInterval = null;
let stateStartTime = Date.now();
let recordingTimeout = null;
var RECORDING_LIMIT_MS = 30000;

// Pull history state
var pullHistory = { loaded: false, timestamps: {} };


// Sound effects
var sfx = {
  record: new Audio("/static/sounds/record.wav"),
  ping: new Audio("/static/sounds/ping.wav"),
  error: new Audio("/static/sounds/error.wav"),
  thinking: new Audio("/static/sounds/thinking.wav"),
  affirmative: new Audio("/static/sounds/affirmative.wav"),
  negatory: new Audio("/static/sounds/negatory.wav"),
  working: new Audio("/static/sounds/working.wav")
};
sfx.thinking.loop = true;
sfx.working.loop = true;
var sfxUnlocked = false;

function unlockSfx() {
  if (sfxUnlocked) return;
  sfxUnlocked = true;
  Object.keys(sfx).forEach(function(key) {
    sfx[key].load();
  });
}
document.addEventListener("click", unlockSfx, { once: true });
document.addEventListener("keydown", unlockSfx, { once: true });

function playSound(name) {
  var sound = sfx[name];
  if (!sound) return;
  sound.currentTime = 0;
  sound.play().catch(function() {});
}

function stopSound(name) {
  var sound = sfx[name];
  if (!sound) return;
  sound.pause();
  sound.currentTime = 0;
}

function stopAllSounds() {
  Object.keys(sfx).forEach(function(key) {
    sfx[key].pause();
    sfx[key].currentTime = 0;
  });
}

// ============================================
// THEME TOGGLE
// ============================================

(function initThemeToggle() {
  var checkbox = document.getElementById("themeToggleInput");
  var themeSheet = document.getElementById("themeSheet");
  var vrgbTokens = document.getElementById("vrgb-tokens");
  var stored = localStorage.getItem("cue-vox-theme");
  var isOn = (stored !== "off");

  function applyThemeState(on) {
    themeSheet.disabled = !on;
    if (!on) {
      vrgbTokens.textContent = "";
    } else if (window._spectraCSS) {
      vrgbTokens.textContent = window._spectraCSS;
    }
    checkbox.checked = on;
  }

  applyThemeState(isOn);

  checkbox.addEventListener("change", function() {
    var on = checkbox.checked;
    localStorage.setItem("cue-vox-theme", on ? "on" : "off");
    applyThemeState(on);
  });
})();

// Modifier token state
var modifierTokens = {};          // target_id -> DOM thumbnail element
var modifiersByTarget = {};       // target_id -> [modifier_ids]
var modifierThermalData = {};     // target_id -> {temperature, base_temp, cooling_rate, created_at}
var tokenRegistry = {};
var currentLightboxTokenId = null;

// ============================================
// Drawer Toggle
// ============================================

drawerToggle.addEventListener('click', () => {
  drawer.classList.toggle('open');
});

// Close drawer when clicking outside
document.addEventListener('click', (e) => {
  if (drawer.classList.contains('open') &&
      !drawer.contains(e.target) &&
      !drawerToggle.contains(e.target)) {
    drawer.classList.remove('open');
  }
});

// ============================================
// Audio Recording
// ============================================

async function initAudio() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    mediaRecorder = new MediaRecorder(stream);

    mediaRecorder.ondataavailable = (event) => {
      audioChunks.push(event.data);
    };

    mediaRecorder.onstop = async () => {
      const audioBlob = new Blob(audioChunks, { type: 'audio/wav' });
      const reader = new FileReader();
      reader.readAsDataURL(audioBlob);
      reader.onloadend = () => {
        socket.emit('audio_data', { audio: reader.result });
      };
      audioChunks = [];
    };

    console.log('✅ Microphone initialized');
  } catch (err) {
    console.error('❌ Microphone access denied:', err);
    addSystemMessage('Microphone access denied. Please enable microphone permissions.');
  }
}

// ============================================
// Keyboard Events (Spacebar recording)
// ============================================

document.addEventListener('keydown', (e) => {
  // Don't trigger if typing in any text input or textarea
  if (e.target === drawerTextInput || e.target.matches('textarea, input[type="text"]')) return;

  if (e.code === 'Space' && !isRecording) {
    e.preventDefault();

    // Block recording when gallery lightbox is open
    if (galleryLightboxOpen) return;

    // Block if there's pending input
    if (hasPendingInput) {
      console.log('⚠️ Please answer the question first');
      return;
    }

    if (currentState === 'speaking') {
      stopAllSounds();
      socket.emit('interrupt');
      return;
    }

    if (!mediaRecorder) {
      console.error('❌ MediaRecorder not initialized');
      return;
    }

    console.log('🎙️ Starting recording...');
    isRecording = true;
    setState('recording');
    mediaRecorder.start(1000);

    // 30-second recording limit
    recordingTimeout = setTimeout(function() {
      if (isRecording) {
        console.log('⏱️ 30s recording limit reached');
        stopRecording();
      }
    }, RECORDING_LIMIT_MS);
  }
});

function stopRecording() {
  if (!isRecording) return;
  console.log('⏹️ Stopping recording...');
  playSound("ping");
  isRecording = false;
  if (recordingTimeout) {
    clearTimeout(recordingTimeout);
    recordingTimeout = null;
  }
  setState('transcribing');

  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.stop();
  } else {
    console.error('❌ MediaRecorder not active');
  }
}

document.addEventListener('keyup', (e) => {
  // Don't trigger if typing in any text input or textarea
  if (e.target === drawerTextInput || e.target.matches('textarea, input[type="text"]')) return;

  if (e.code === 'Space' && isRecording) {
    e.preventDefault();
    stopRecording();
  }
});

// ============================================
// Text Input
// ============================================

function sendTextMessage() {
  const text = drawerTextInput.value.trim();
  if (!text) return;
  playSound("affirmative");

  // Block if there's pending input
  if (hasPendingInput) {
    console.log('⚠️ Please answer the question first');
    return;
  }

  // Add user message immediately (backend doesn't echo text input)
  addMessage('user', text);
  socket.emit('text_message', { text: text });
  drawerTextInput.value = '';
}

drawerSendButton.addEventListener('click', sendTextMessage);

drawerTextInput.addEventListener('keypress', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendTextMessage();
  }
});

// ============================================
// Stop Audio Buttons
// ============================================

canvasStopBtn.addEventListener('click', () => {
  stopAllSounds();
  socket.emit('interrupt');
});

drawerStopLink.addEventListener('click', (e) => {
  e.preventDefault();
  stopAllSounds();
  socket.emit('interrupt');
});

// ============================================
// Socket Events
// ============================================

socket.on('state_change', (data) => {
  console.log('🔄 State change:', data.state);
  setState(data.state);
});

// ============================================
// FOLLOW-UP STATE MACHINE
// General-purpose: gather context → propose → YES/NO gate → commit or loop
// ============================================
window._followUp = null;

function startFollowUp(config) {
  // config: { type, context, generate(userInput, cb), onConfirm(proposal), prompt }
  window._followUp = {
    type: config.type,
    context: config.context || {},
    generate: config.generate,
    onConfirm: config.onConfirm,
    phase: "gather",   // gather → proposed → (confirm or loop)
    proposal: null
  };
  console.log("[follow-up] started: " + config.type);
}

function _followUpPropose(proposal) {
  var fu = window._followUp;
  if (!fu) return;
  fu.phase = "proposed";
  fu.proposal = proposal;

  // Show proposal with YES/NO gate
  var card = document.createElement("article");
  card.className = "card assistant";
  card.dataset.timestamp = Date.now();
  var body = document.createElement("div");
  body.className = "card__body";

  var p = document.createElement("p");
  p.textContent = proposal;
  body.appendChild(p);

  var question = document.createElement("p");
  question.className = "card__description";
  question.textContent = "update to this?";
  body.appendChild(question);

  var btnGroup = document.createElement("div");
  btnGroup.className = "button-group";

  var yesBtn = document.createElement("button");
  yesBtn.className = "btn btn--primary";
  yesBtn.textContent = "Yes";
  yesBtn.addEventListener("click", function(e) {
    e.stopPropagation();
    playSound("affirmative");
    yesBtn.disabled = true;
    noBtn.disabled = true;
    addMessage("user", "Yes");
    if (fu.onConfirm) fu.onConfirm(fu.proposal);
    window._followUp = null;
    console.log("[follow-up] confirmed");
  });

  var noBtn = document.createElement("button");
  noBtn.className = "btn btn--secondary";
  noBtn.textContent = "No";
  noBtn.addEventListener("click", function(e) {
    e.stopPropagation();
    playSound("negatory");
    yesBtn.disabled = true;
    noBtn.disabled = true;
    addMessage("user", "No");
    // Loop back to gather phase
    fu.phase = "gather";
    fu.proposal = null;
    var loopPrompt = "ok, tell me more -- what should change?";
    addMessage("assistant", loopPrompt);
    socket.emit("speak", { text: loopPrompt });
    console.log("[follow-up] rejected, looping");
  });

  btnGroup.appendChild(yesBtn);
  btnGroup.appendChild(noBtn);
  body.appendChild(btnGroup);
  card.appendChild(body);

  conversation.appendChild(card);
  conversation.scrollTop = conversation.scrollHeight;

  // TTS the proposal
  socket.emit("speak", { text: proposal + ". update to this?" });
}

socket.on('transcription', (data) => {
  console.log('Transcription received:', data.text);
  addMessage('user', data.text);

  // Follow-up intercept
  if (window._followUp && window._followUp.phase === "gather") {
    var fu = window._followUp;

    // Show processing
    var card = document.createElement("article");
    card.className = "card assistant";
    card.dataset.timestamp = Date.now();
    var body = document.createElement("div");
    body.className = "card__body";
    var statusText = document.createElement("p");
    statusText.className = "drop-status";
    statusText.textContent = "thinking...";
    body.appendChild(statusText);
    card.appendChild(body);
    conversation.appendChild(card);
    conversation.scrollTop = conversation.scrollHeight;

    // Call the generator with user input
    fu.generate(data.text, function(proposal) {
      statusText.remove();
      if (proposal) {
        _followUpPropose(proposal);
      } else {
        var failBody = document.createElement("p");
        failBody.textContent = "couldn't generate -- try again?";
        body.appendChild(failBody);
        socket.emit("speak", { text: "couldn't generate. try again?" });
      }
    });

    socket.emit("interrupt");
    return;
  }
});

socket.on('response', (data) => {
  console.log('🤖 Response received:', data.text.substring(0, 50) + '...');
  stopSound("thinking");
  addMessage('assistant', data.text, data.tts_chunks || null);

  // Retroactively mark the last user bubble with SNR dot
  if (data.snr_hex) {
    var userCards = conversation.querySelectorAll(".card.user");
    var lastUser = userCards[userCards.length - 1];
    if (lastUser) {
      var body = lastUser.querySelector(".card__body");
      if (body && !body.querySelector(".snr-dot")) {
        var dot = document.createElement("span");
        dot.className = "snr-dot";
        dot.style.backgroundColor = data.snr_hex;
        dot.setAttribute("aria-hidden", "true");
        body.appendChild(dot);
      }
    }
  }
});

socket.on('error', (data) => {
  console.error('❌ Socket error:', data.message);
  stopSound("thinking");
  playSound("error");
  addSystemMessage('Error: ' + data.message);
  setState('idle');
});

// ============================================
// Pull History
// ============================================

var pullTrigger = document.getElementById("pullTrigger");

if (pullTrigger) {
  pullTrigger.addEventListener("click", function() {
    if (pullHistory.loaded) return;
    pullTrigger.textContent = "pulling...";
    pullTrigger.disabled = true;
    socket.emit("pull_history");
  });
}

socket.on("pull_history_result", function(data) {
  var entries = data.entries || [];

  if (entries.length === 0) {
    if (pullTrigger) {
      pullTrigger.textContent = "no history";
      setTimeout(function() {
        pullTrigger.textContent = "pull";
        pullTrigger.disabled = false;
      }, 2000);
    }
    return;
  }

  pullHistory.loaded = true;

  // Build fragment with all pulled messages
  var fragment = document.createDocumentFragment();

  for (var i = 0; i < entries.length; i++) {
    var entry = entries[i];
    var ts = entry.timestamp || "";

    // Skip duplicates
    if (pullHistory.timestamps[ts]) continue;
    pullHistory.timestamps[ts] = true;

    // User message
    if (entry.user) {
      fragment.appendChild(createPulledMessage("user", entry.user, ts));
    }
    // Assistant message
    if (entry.assistant) {
      fragment.appendChild(createPulledMessage("assistant", entry.assistant, ts));
    }
  }

  // Add separator
  var sep = document.createElement("div");
  sep.className = "pull-separator";
  sep.textContent = "now";
  fragment.appendChild(sep);

  // Insert after the pull button, before any live cards
  var firstCard = conversation.querySelector(".card");
  if (firstCard) {
    conversation.insertBefore(fragment, firstCard);
  } else {
    conversation.appendChild(fragment);
  }

  // Scroll to bottom so most recent messages are visible
  conversation.scrollTop = conversation.scrollHeight;

  // Mark button as done
  if (pullTrigger) {
    pullTrigger.textContent = "pulled";
    pullTrigger.classList.add("pull-trigger--done");
  }
});

socket.on("tts_chunk_start", function(data) {
  var prev = document.querySelector(".tts-speaking");
  if (prev) prev.classList.remove("tts-speaking");

  var cards = conversation.querySelectorAll(".card.assistant");
  var lastCard = cards[cards.length - 1];
  if (!lastCard) return;

  var speakable = lastCard.querySelectorAll(".tts-speakable");
  if (data.index < speakable.length) {
    speakable[data.index].classList.add("tts-speaking");
    speakable[data.index].scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
});

socket.on("tts_chunk_done", function() {
  var active = document.querySelector(".tts-speaking");
  if (active) active.classList.remove("tts-speaking");
  playSound("negatory");
});

// ============================================
// State Management
// ============================================

function formatElapsed(ms) {
  var secs = Math.floor(ms / 1000);
  if (secs < 60) return secs + "s";
  var mins = Math.floor(secs / 60);
  var remSecs = secs % 60;
  if (mins < 60) return mins + "m " + remSecs + "s";
  var hrs = Math.floor(mins / 60);
  var remMins = mins % 60;
  return hrs + "h " + remMins + "m";
}

var thinkingPhases = [
  "thinking",
  "parsing",
  "reading",
  "searching",
  "running tools",
  "composing",
  "reviewing"
];
var lastThinkingPhase = "thinking";

function getThinkingPhase(elapsed) {
  var secs = Math.floor(elapsed / 1000);
  if (secs < 2) return "thinking";
  if (secs < 5) return "parsing";
  if (secs < 10) return "reading";
  if (secs < 18) return "searching";
  if (secs < 30) return "running tools";
  if (secs < 50) return "composing";
  return "reviewing";
}

function updateStatusTimer() {
  var label;
  if (currentState === "recording") {
    var remaining = RECORDING_LIMIT_MS - (Date.now() - stateStartTime);
    if (remaining < 0) remaining = 0;
    label = currentState + " " + formatElapsed(remaining);
  } else if (currentState === "thinking") {
    var elapsed = Date.now() - stateStartTime;
    var phase = getThinkingPhase(elapsed);
    if (phase !== "thinking" && lastThinkingPhase === "thinking") {
      console.log("[SFX] starting working sound at phase:", phase);
      playSound("working");
    }
    lastThinkingPhase = phase;
    label = "thinking " + formatElapsed(elapsed) + (phase !== "thinking" ? " (" + phase + "...)" : "");
  } else {
    var elapsed = Date.now() - stateStartTime;
    label = currentState + " " + formatElapsed(elapsed);
  }
  if (canvasStatus) canvasStatus.textContent = label;
  if (drawerStatusText) drawerStatusText.textContent = label;
}

function setState(state) {
  currentState = state;
  stateStartTime = Date.now();

  // Sound effects per state
  stopSound("thinking");
  stopSound("working");
  lastThinkingPhase = "thinking";
  if (state === "recording") {
    playSound("record");
  } else if (state === "thinking") {
    playSound("thinking");
  }

  // Clear previous timer
  if (stateTimerInterval) {
    clearInterval(stateTimerInterval);
    stateTimerInterval = null;
  }

  // Clear recording timeout if leaving recording state
  if (state !== "recording" && recordingTimeout) {
    clearTimeout(recordingTimeout);
    recordingTimeout = null;
  }

  // Update state dot
  if (stateDot) {
    stateDot.setAttribute('data-state', state);
  }

  // Update drawer status
  if (drawerStatusDot) {
    drawerStatusDot.setAttribute('data-state', state);
  }

  // Set initial status text
  var initialLabel = state === "recording" ? state + " 30s" : state + " 0s";
  if (drawerStatusText) {
    drawerStatusText.textContent = initialLabel;
  }

  // Update canvas status
  if (canvasStatus) {
    canvasStatus.textContent = initialLabel;

    if (state === 'recording' || state === 'transcribing' || state === 'thinking') {
      canvasStatus.classList.add('active');
    } else {
      canvasStatus.classList.remove('active');
    }
  }

  // Start upcount timer
  stateTimerInterval = setInterval(updateStatusTimer, 1000);

  // Show/hide stop audio controls
  if (canvasStopBtn && drawerStopLink) {
    if (state === 'speaking') {
      canvasStopBtn.style.display = 'block';
      drawerStopLink.style.display = 'block';
    } else {
      canvasStopBtn.style.display = 'none';
      drawerStopLink.style.display = 'none';
    }
  }
}

// ============================================
// Message Rendering (Haberdash Cards)
// ============================================

function addMessage(role, text, ttsChunks) {
  // Create a simple hash for deduplication
  const messageKey = `${role}:${text.substring(0, 50)}`;
  const now = Date.now();

  // Prevent adding the exact same message twice in a row within 1 second
  if (lastMessageHash) {
    const [lastKey, lastTime] = lastMessageHash.split('|');
    if (lastKey === messageKey && (now - parseInt(lastTime)) < 1000) {
      console.warn('Duplicate message blocked:', text.substring(0, 50));
      return;
    }
  }
  lastMessageHash = `${messageKey}|${now}`;

  const messageCard = document.createElement('article');
  messageCard.className = `card ${role}`;
  messageCard.dataset.timestamp = Date.now();
  messageCard.dataset.rawText = text;

  const header = document.createElement('header');
  header.className = 'card__header';

  const icon = document.createElement('span');
  icon.className = 'card__icon';
  icon.setAttribute('aria-hidden', 'true');
  icon.textContent = role === 'user' ? '\u{1F464}' : '\u{1F916}';

  const title = document.createElement('h3');
  title.className = 'card__title';
  title.textContent = role === 'user' ? 'You' : 'Assistant';

  header.appendChild(icon);
  header.appendChild(title);

  const body = document.createElement('div');
  body.className = 'card__body';

  // Render message with embedded structured content
  body.dataset.role = role;
  renderMessageContent(body, text);

  // If backend sent TTS chunks, render each chunk as its own markdown block
  // so index matching with backend tts_chunk_start events is guaranteed
  if (role === "assistant" && ttsChunks && ttsChunks.length > 0) {
    var mdContent = body.querySelector(".markdown-content");
    if (mdContent) mdContent.remove();
    for (var i = 0; i < ttsChunks.length; i++) {
      var wrapper = document.createElement("div");
      wrapper.className = "tts-speakable";
      wrapper.addEventListener("click", handleTTSClick);
      renderMarkdownInto(wrapper, ttsChunks[i]);
      body.appendChild(wrapper);
    }
  }

  // Add timestamp
  const timestamp = document.createElement('div');
  timestamp.className = 'message-timestamp';
  timestamp.textContent = 'just now';
  body.appendChild(timestamp);

  // Emoji reactions (assistant messages only)
  if (role === "assistant") {
  var reactions = document.createElement("ul");
  reactions.className = "emoji-reactions";
  var emojis = ["\uD83D\uDC4D", "\u2764\uFE0F", "\uD83D\uDE02", "\uD83E\uDD14", "\uD83D\uDD25"];
  for (var e = 0; e < emojis.length; e++) {
    (function(emoji) {
      var li = document.createElement("li");
      var btn = document.createElement("button");
      btn.className = "emoji-reactions__btn";
      btn.textContent = emoji;
      btn.addEventListener("click", function() {
        playSound("affirmative");
        socket.emit("emoji_reaction", { emoji: emoji, role: role });
        var card = btn.closest(".card");
        if (!card) return;
        var badge = card.querySelector(".emoji-badge");
        if (!badge) {
          badge = document.createElement("span");
          badge.className = "emoji-badge";
          badge.dataset.emojis = "";
          var ts = card.querySelector(".message-timestamp");
          if (ts) {
            ts.parentNode.insertBefore(badge, ts);
          } else {
            card.querySelector(".card__body").appendChild(badge);
          }
        }
        var list = badge.dataset.emojis ? badge.dataset.emojis.split(",") : [];
        list.push(emoji);
        badge.dataset.emojis = list.join(",");
        var display = list.slice(0, 3).join("");
        if (list.length > 3) display += "\u2026";
        badge.textContent = display;
      });
      li.appendChild(btn);
      reactions.appendChild(li);
    })(emojis[e]);
  }
  body.appendChild(reactions);
  } // end assistant-only reactions

  messageCard.appendChild(header);
  messageCard.appendChild(body);

  conversation.appendChild(messageCard);
  conversation.scrollTop = conversation.scrollHeight;
}

// ============================================
// Pulled Message Helpers
// ============================================

function createPulledMessage(role, text, isoTimestamp) {
  var card = document.createElement("article");
  card.className = "card " + role + " card--pulled";
  card.dataset.timestamp = isoTimestamp;

  var header = document.createElement("header");
  header.className = "card__header";
  var icon = document.createElement("span");
  icon.className = "card__icon";
  icon.setAttribute("aria-hidden", "true");
  var title = document.createElement("h3");
  title.className = "card__title";
  title.textContent = role === "user" ? "You" : "Assistant";
  header.appendChild(icon);
  header.appendChild(title);

  var body = document.createElement("div");
  body.className = "card__body";

  // Truncate long messages
  var truncateAt = 500;
  if (text.length > truncateAt) {
    var preview = document.createElement("div");
    preview.className = "markdown-content";
    preview.textContent = text.substring(0, truncateAt) + "...";

    var full = document.createElement("div");
    full.className = "markdown-content";
    full.textContent = text;
    full.style.display = "none";

    var expandBtn = document.createElement("button");
    expandBtn.className = "pull-expand";
    expandBtn.textContent = "show more";
    expandBtn.addEventListener("click", function() {
      if (full.style.display === "none") {
        full.style.display = "";
        preview.style.display = "none";
        expandBtn.textContent = "show less";
      } else {
        full.style.display = "none";
        preview.style.display = "";
        expandBtn.textContent = "show more";
      }
    });

    body.appendChild(preview);
    body.appendChild(full);
    body.appendChild(expandBtn);
  } else {
    var content = document.createElement("div");
    content.className = "markdown-content";
    content.textContent = text;
    body.appendChild(content);
  }

  // Relative timestamp
  var ts = document.createElement("div");
  ts.className = "message-timestamp";
  ts.textContent = formatRelativeTime(isoTimestamp);
  body.appendChild(ts);

  card.appendChild(header);
  card.appendChild(body);
  return card;
}

function formatRelativeTime(isoTimestamp) {
  if (!isoTimestamp) return "";
  try {
    // Handle truncated timestamps like "2026-02-28T14:13"
    var d = new Date(isoTimestamp);
    if (isNaN(d.getTime())) return "";
    var diff = Math.floor((Date.now() - d.getTime()) / 1000);
    if (diff < 60) return "just now";
    if (diff < 3600) return Math.floor(diff / 60) + "m ago";
    if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
    return "yesterday";
  } catch (e) {
    return "";
  }
}

// ============================================
// Widget Registry
// ============================================
// Register widget creators by tag type. New types are added by registering
// a function here - no parser changes needed.

const widgetRegistry = {
  // YES_NO: plain text question -> binary buttons
  YES_NO: function(data) {
    return createYesNoQuestion(data);
  },

  // APPROVAL: JSON -> approve/reject gate
  APPROVAL: function(data) {
    var approvalData = JSON.parse(data);
    return createApprovalGate(approvalData);
  },

  // INPUT: JSON -> dispatches to sub-type widgets (slider, text, etc.)
  INPUT: function(data) {
    var inputData = JSON.parse(data);
    var subtype = inputData.type;
    if (inputWidgetRegistry[subtype]) {
      return inputWidgetRegistry[subtype](inputData);
    }
    console.warn("Unknown input type:", subtype);
    return null;
  },

  // DOCUMENT: JSON -> co-managed document editor
  DOCUMENT: function(data) {
    var docData = JSON.parse(data);
    return createDocumentEditor(docData);
  },

  // CUE: JSON -> executable cue card with approval gate
  CUE: function(data) {
    var cueData = JSON.parse(data);
    return createCueCard(cueData);
  },

  // GALLERY: JSON -> inline thumbnail strip with lightbox
  GALLERY: function(data) {
    var galleryData;
    try {
      galleryData = JSON.parse(data);
    } catch (e) {
      console.error("[gallery] failed to parse GALLERY JSON: " + e.message);
      console.error("[gallery] raw data preview: " + String(data).substring(0, 200));
      return null;
    }
    if (!galleryData || !Array.isArray(galleryData.images)) {
      console.warn("[gallery] GALLERY data missing images array: " + JSON.stringify(galleryData).substring(0, 200));
      return null;
    }
    return createGalleryStrip(galleryData);
  }
};

// Sub-registry for INPUT type dispatching
const inputWidgetRegistry = {
  slider: function(inputData) {
    return createSemanticSlider(inputData);
  },
  text: function(inputData) {
    return createTextInput(inputData);
  },
  yes_no: function(inputData) {
    return createYesNoInput(inputData);
  },
  choice: function(inputData) {
    return createChoiceInput(inputData);
  }
};


// Configure marked.js for safe rendering
if (typeof marked !== "undefined") {
  marked.use({
    breaks: true,
    gfm: true,
    async: false
  });
}

// Render markdown text into a container element
function renderMarkdown(container, text) {
  if (!text) return;

  if (typeof marked !== "undefined") {
    try {
      var div = document.createElement("div");
      div.className = "markdown-content";
      var rendered = marked.parse(String(text), { async: false });
      // Guard against accidental async (Promise returned)
      if (typeof rendered === "string") {
        div.innerHTML = rendered;
      } else {
        div.textContent = text;
      }
      container.appendChild(div);
      wireUpTTSClicks(container, div);
    } catch (err) {
      console.error("Markdown render error:", err);
      var p = document.createElement("p");
      p.className = "card__description markdown-content";
      p.textContent = text;
      container.appendChild(p);
    }
  } else {
    var p = document.createElement("p");
    p.className = "card__description";
    p.textContent = text;
    container.appendChild(p);
  }
}

function renderMarkdownInto(el, text) {
  if (!text) return;
  if (typeof marked !== "undefined") {
    try {
      var rendered = marked.parse(String(text), { async: false });
      if (typeof rendered === "string") {
        el.innerHTML = rendered;
      } else {
        el.textContent = text;
      }
    } catch (err) {
      el.textContent = text;
    }
  } else {
    el.textContent = text;
  }
}

function wireUpTTSClicks(container, markdownDiv) {
  // Only wire clicks on assistant messages
  if (container.dataset.role !== "assistant") return;

  var speakable = markdownDiv.querySelectorAll("p, li, h1, h2, h3, h4, blockquote");
  for (var i = 0; i < speakable.length; i++) {
    var el = speakable[i];
    el.classList.add("tts-speakable");
    el.addEventListener("click", handleTTSClick);
  }
}

function handleTTSClick(e) {
  // Don't fire TTS if user is selecting text
  var selection = window.getSelection();
  if (selection && selection.toString().length > 0) return;

  var text = e.currentTarget.textContent.trim();
  if (!text) return;

  // Visual feedback
  var active = document.querySelector(".tts-speaking");
  if (active) active.classList.remove("tts-speaking");
  e.currentTarget.classList.add("tts-speaking");

  socket.emit("narrate_caption", { text: text });
}

// Render message content with embedded structured components
function renderMessageContent(container, text) {
  console.log("Rendering message:", text.substring(0, 100) + (text.length > 100 ? "..." : ""));

  // Find structured tags with bracket-balanced matching
  // Simple regex fails when JSON payload contains ] (e.g. arrays in choice options)
  var tagStartRegex = /\[(YES_NO|INPUT|APPROVAL|DOCUMENT|CUE|GALLERY|CITATIONS):\s*/g;

  var matches = [];
  var match;

  while ((match = tagStartRegex.exec(text)) !== null) {
    var tagType = match[1];
    var dataStart = match.index + match[0].length;
    // Walk forward counting brackets to find the balanced closing ]
    var depth = 1;  // We are inside the opening [
    var pos = dataStart;
    while (pos < text.length && depth > 0) {
      if (text[pos] === "[") depth++;
      else if (text[pos] === "]") depth--;
      if (depth > 0) pos++;
    }
    if (depth === 0) {
      var data = text.substring(dataStart, pos);
      matches.push({
        type: tagType,
        index: match.index,
        length: pos - match.index + 1,
        data: data
      });
    }
  }

  // Sort by position
  matches.sort(function(a, b) { return a.index - b.index; });

  var lastIndex = 0;
  var hasContent = matches.length > 0;

  // Detect batch mode: multiple input-type tags (INPUT or YES_NO) in one message
  var inputTypes = { INPUT: true, YES_NO: true };
  var inputCount = matches.filter(function(m) { return inputTypes[m.type]; }).length;
  var batchMode = inputCount > 1;

  // Process each match in order
  matches.forEach(function(m) {
    console.log("Detected " + m.type + " tag at position " + m.index);

    // Add text before the tag
    var textBefore = text.substring(lastIndex, m.index).trim();
    if (textBefore) {
      renderMarkdown(container, textBefore);
    }

    // Look up widget creator from registry
    var creator = widgetRegistry[m.type];
    if (creator) {
      try {
        var creatorData = m.data;
        if (batchMode) {
          if (m.type === "INPUT") {
            // Inject batch flag into INPUT JSON
            var parsed = JSON.parse(m.data);
            parsed._batch = true;
            creatorData = JSON.stringify(parsed);
          } else if (m.type === "YES_NO") {
            // Wrap YES_NO text with batch signal prefix
            creatorData = "__BATCH__" + m.data;
          }
        }
        var widget = creator(creatorData);
        if (widget) {
          container.appendChild(widget);
        }
      } catch (e) {
        console.error("Failed to create " + m.type + " widget:", e);
        var errorP = document.createElement("p");
        errorP.className = "card__description";
        errorP.textContent = "[" + m.type + ": " + m.data + "]";
        errorP.style.color = "var(--error, #ff4444)";
        container.appendChild(errorP);
      }
    } else {
      console.warn("No widget registered for tag type:", m.type);
    }

    lastIndex = m.index + m.length;
  });

  // If no structured content was found, render as markdown
  if (!hasContent) {
    renderMarkdown(container, text);
  } else {
    // Add any remaining text after the last tag
    var textAfter = text.substring(lastIndex).trim();
    if (textAfter) {
      renderMarkdown(container, textAfter);
    }
  }

  // In batch mode, add a single "Submit All" button
  if (batchMode) {
    setPendingInput(true);
    var submitAllBtn = document.createElement("button");
    submitAllBtn.className = "btn btn--primary";
    submitAllBtn.textContent = "Cue All";
    submitAllBtn.style.marginTop = "var(--space-lg, 1rem)";
    submitAllBtn.addEventListener("click", function() {
      playSound("affirmative");
      var allFilled = true;
      var combinedMessage = [];

      // Collect from all batch widgets inside this message container
      var batchWidgets = container.querySelectorAll(".structured-question");
      batchWidgets.forEach(function(widget) {
        var batchType = widget.dataset.batchType;
        var question = widget.querySelector(".card__description").textContent;

        if (batchType === "yes_no") {
          // Yes/No toggle
          var val = widget.dataset.batchValue;
          if (!val) {
            allFilled = false;
            widget.classList.add("error");
          } else {
            widget.classList.remove("error");
            combinedMessage.push(question + ": " + val);
          }
        } else if (batchType === "slider") {
          // Slider -- always has a value (default 50)
          var slider = widget.querySelector(".slider-input");
          if (slider) {
            combinedMessage.push(question + ": " + slider.value);
          }
        } else {
          // Text input
          var ta = widget.querySelector("textarea.text-input");
          if (ta) {
            if (!ta.value.trim()) {
              allFilled = false;
              ta.classList.add("error");
            } else {
              ta.classList.remove("error");
              combinedMessage.push(question + ": " + ta.value.trim());
            }
          }
        }
      });
      if (!allFilled) return;

      var messageText = combinedMessage.join("\n");

      // Disable all inputs
      container.querySelectorAll("textarea.text-input").forEach(function(ta) {
        ta.disabled = true;
        ta.classList.add("disabled");
      });
      container.querySelectorAll(".slider-input").forEach(function(sl) {
        sl.disabled = true;
      });
      container.querySelectorAll(".batch-toggle").forEach(function(btn) {
        btn.disabled = true;
        btn.classList.add("disabled");
      });
      submitAllBtn.remove();
      setPendingInput(false);

      // Send as single text_message -- one Claude call, one response
      addMessage("user", messageText);
      socket.emit("text_message", { text: messageText });
    });
    container.appendChild(submitAllBtn);
  }
}

// Create modifier icon for structured question cards
function createModifierIcon() {
  var btn = document.createElement("button");
  btn.className = "pin-icon";
  btn.setAttribute("data-state", "inactive");
  btn.style.display = "none"; // hidden until token_created assigns an ID
  btn.setAttribute("aria-label", "Hold token");
  btn.setAttribute("title", "hold on");

  var glyph = document.createElement("span");
  glyph.className = "pin-icon__glyph";
  btn.appendChild(glyph);

  btn.addEventListener("click", function(e) {
    e.stopPropagation();
    var tokenId = btn.dataset.tokenId;
    if (!tokenId) return;

    var state = btn.getAttribute("data-state");
    if (state === "inactive" || state === "sleeping") {
      playSound("affirmative");
      socket.emit("create_modifier", { token_id: tokenId });
      btn.setAttribute("data-state", "active");
      var card = btn.closest(".structured-question");
      createModifierThumbnail(tokenId, card);
    } else {
      socket.emit("remove_modifier", { token_id: tokenId });
      btn.setAttribute("data-state", "inactive");
      removeModifierThumbnail(tokenId);
    }
  });

  return btn;
}

// Create YES/NO question UI
function createYesNoQuestion(questionText) {
  // Detect batch mode via prefix
  var isBatch = questionText.indexOf("__BATCH__") === 0;
  if (isBatch) {
    questionText = questionText.replace("__BATCH__", "");
  }

  var container = document.createElement("div");
  container.className = "structured-question";

  var question = document.createElement("p");
  question.className = "card__description";
  question.textContent = questionText;
  container.appendChild(question);

  var buttonGroup = document.createElement("div");
  buttonGroup.className = "button-group";
  buttonGroup.style.display = "flex";
  buttonGroup.style.gap = "var(--space-sm, 0.5rem)";
  buttonGroup.style.marginTop = "var(--space-md, 0.75rem)";

  if (isBatch) {
    // Batch mode: toggle buttons, no immediate submit
    var yesBtn = document.createElement("button");
    yesBtn.className = "btn btn--secondary batch-toggle";
    yesBtn.textContent = "Yes";
    yesBtn.dataset.batchQuestion = questionText;
    yesBtn.dataset.batchValue = "";

    var noBtn = document.createElement("button");
    noBtn.className = "btn btn--secondary batch-toggle";
    noBtn.textContent = "No";
    noBtn.dataset.batchQuestion = questionText;
    noBtn.dataset.batchValue = "";

    yesBtn.addEventListener("click", function(e) {
      e.stopPropagation();
      playSound("affirmative");
      yesBtn.className = "btn btn--primary batch-toggle";
      noBtn.className = "btn btn--secondary batch-toggle";
      yesBtn.dataset.batchValue = "Yes";
      noBtn.dataset.batchValue = "";
      container.dataset.batchValue = "Yes";
    });
    noBtn.addEventListener("click", function(e) {
      e.stopPropagation();
      playSound("negatory");
      noBtn.className = "btn btn--primary batch-toggle";
      yesBtn.className = "btn btn--secondary batch-toggle";
      noBtn.dataset.batchValue = "No";
      yesBtn.dataset.batchValue = "";
      container.dataset.batchValue = "No";
    });

    buttonGroup.appendChild(yesBtn);
    buttonGroup.appendChild(noBtn);
    container.appendChild(buttonGroup);
    container.dataset.batchType = "yes_no";
    container.dataset.batchQuestion = questionText;
    container.dataset.batchValue = "";
  } else {
    // Normal mode: immediate submit
    var yesBtn = document.createElement("button");
    yesBtn.className = "btn btn--primary";
    yesBtn.textContent = "Yes";
    yesBtn.addEventListener("click", function(e) {
      e.stopPropagation();
      handleQuestionResponse("Yes", buttonGroup, questionText);
    });

    var noBtn = document.createElement("button");
    noBtn.className = "btn btn--secondary";
    noBtn.textContent = "No";
    noBtn.addEventListener("click", function(e) {
      e.stopPropagation();
      handleQuestionResponse("No", buttonGroup, questionText);
    });

    buttonGroup.appendChild(yesBtn);
    buttonGroup.appendChild(noBtn);
    container.appendChild(buttonGroup);
    setPendingInput(true);
  }

  container.appendChild(createModifierIcon());
  return container;
}

// Handle question response
function handleQuestionResponse(answer, buttonGroup, questionText) {
  playSound(answer === "No" ? "negatory" : "affirmative");
  // Send as button_response so backend creates YES/NO token
  socket.emit("button_response", { answer: answer });

  // Disable all buttons in the group
  const buttons = buttonGroup.querySelectorAll('button');
  buttons.forEach(btn => {
    btn.disabled = true;
    btn.classList.add('disabled');
  });

  // Add choice indicator after the button group
  const choiceIndicator = document.createElement('div');
  choiceIndicator.className = 'choice-indicator';
  choiceIndicator.textContent = `Selected: ${answer}`;

  buttonGroup.parentNode.appendChild(choiceIndicator);

  // Unblock input
  setPendingInput(false);

  // Add user message immediately (backend doesn't echo button responses)
  addMessage("user", answer);
}

// Create yes/no input from INPUT JSON (type: "yes_no")
function createYesNoInput(inputData) {
  var container = document.createElement("div");
  container.className = "structured-question";

  var question = document.createElement("p");
  question.className = "card__description";
  question.textContent = inputData.question;
  container.appendChild(question);

  var buttonGroup = document.createElement("div");
  buttonGroup.className = "button-group";

  var yesBtn = document.createElement("button");
  yesBtn.className = "btn btn--primary";
  yesBtn.textContent = "Yes";
  yesBtn.addEventListener("click", function(e) {
    e.stopPropagation();
    handleQuestionResponse("Yes", buttonGroup, inputData.question);
  });

  var noBtn = document.createElement("button");
  noBtn.className = "btn btn--secondary";
  noBtn.textContent = "No";
  noBtn.addEventListener("click", function(e) {
    e.stopPropagation();
    handleQuestionResponse("No", buttonGroup, inputData.question);
  });

  buttonGroup.appendChild(yesBtn);
  buttonGroup.appendChild(noBtn);
  container.appendChild(buttonGroup);

  container.appendChild(createModifierIcon());
  setPendingInput(true);

  return container;
}

// Create choice input from INPUT JSON (type: "choice")
function createChoiceInput(inputData) {
  var container = document.createElement("div");
  container.className = "structured-question";

  var question = document.createElement("p");
  question.className = "card__description";
  question.textContent = inputData.question;
  container.appendChild(question);

  var buttonGroup = document.createElement("div");
  buttonGroup.className = "button-group";

  var options = inputData.options || [];
  options.forEach(function(option) {
    var btn = document.createElement("button");
    btn.className = "btn btn--secondary";
    btn.textContent = option.label;
    btn.addEventListener("click", function(e) {
      e.stopPropagation();
      handleQuestionResponse(option.label, buttonGroup, inputData.question);
    });
    buttonGroup.appendChild(btn);
  });

  container.appendChild(buttonGroup);

  container.appendChild(createModifierIcon());
  setPendingInput(true);

  return container;
}

// Create semantic slider (from JSON INPUT)
function createSemanticSlider(inputData) {
  var container = document.createElement("div");
  container.className = "structured-question";

  // Store thermal data for passthrough
  if (inputData.thermal) {
    container.dataset.thermal = JSON.stringify(inputData.thermal);
  }

  var question = document.createElement("p");
  question.className = "card__description";
  question.textContent = inputData.question;
  container.appendChild(question);

  // Slider container
  var sliderContainer = document.createElement("div");
  sliderContainer.className = "slider-container";
  sliderContainer.style.marginTop = "var(--space-md, 0.75rem)";

  // Scale labels (if provided)
  if (inputData.scale) {
    var lowLabel = document.createElement("div");
    lowLabel.className = "slider-label slider-label--low";
    lowLabel.textContent = inputData.scale.low || "Low";
    sliderContainer.appendChild(lowLabel);
  }

  // Slider input (0-100 scale for semantic sliders)
  var slider = document.createElement("input");
  slider.type = "range";
  slider.min = 0;
  slider.max = 100;
  slider.step = 1;
  slider.value = 50;
  slider.className = "slider-input";
  slider.dataset.semanticLabel = inputData.semantic_label || "";

  sliderContainer.appendChild(slider);

  if (inputData.scale) {
    var highLabel = document.createElement("div");
    highLabel.className = "slider-label slider-label--high";
    highLabel.textContent = inputData.scale.high || "High";
    sliderContainer.appendChild(highLabel);
  }

  container.appendChild(sliderContainer);

  if (!inputData._batch) {
    // Normal mode: individual submit
    var submitBtn = document.createElement("button");
    submitBtn.className = "btn btn--primary";
    submitBtn.textContent = "Cue";
    submitBtn.style.marginTop = "var(--space-md, 0.75rem)";
    submitBtn.addEventListener("click", function(e) {
      e.stopPropagation();
      var thermal = container.dataset.thermal ? JSON.parse(container.dataset.thermal) : null;
      handleSliderResponse(slider.value, slider, submitBtn, inputData.semantic_label, inputData.question, thermal);
    });
    container.appendChild(submitBtn);
    setPendingInput(true);
  } else {
    // Batch mode: store metadata for Submit All collection
    container.dataset.batchType = "slider";
    container.dataset.batchQuestion = inputData.question;
  }

  container.appendChild(createModifierIcon());
  return container;
}

// Handle slider response
function handleSliderResponse(value, slider, submitBtn, semanticLabel, question, thermal) {
  playSound(parseInt(value, 10) > 50 ? "affirmative" : "negatory");
  console.log("Slider response:", value, "Label:", semanticLabel);

  // Send the response with context
  var response;
  if (semanticLabel) {
    response = semanticLabel + ": " + value;
  } else if (question) {
    response = "[Response to \"" + question + "\"]: " + value;
  } else {
    response = String(value);
  }

  // Emit as input_response so backend creates a token
  var inputPayload = {
    input: {
      slider_value: parseInt(value, 10),
      semantic_label: semanticLabel || "parameter",
      question: question || ""
    }
  };
  if (thermal) {
    inputPayload.input.thermal = thermal;
  }
  socket.emit("input_response", inputPayload);

  // Disable slider
  slider.disabled = true;
  slider.classList.add("disabled");

  // Remove submit button
  submitBtn.remove();

  // Add choice indicator
  var choiceIndicator = document.createElement("div");
  choiceIndicator.className = "choice-indicator";
  choiceIndicator.textContent = "Selected: " + value + "%";

  slider.parentNode.parentNode.appendChild(choiceIndicator);

  // Unblock input
  setPendingInput(false);

  // Add user message immediately
  addMessage("user", response);
}

// Create text input (from JSON INPUT)
function createTextInput(inputData) {
  var container = document.createElement("div");
  container.className = "structured-question";

  // Store thermal data for passthrough
  if (inputData.thermal) {
    container.dataset.thermal = JSON.stringify(inputData.thermal);
  }

  var question = document.createElement("p");
  question.className = "card__description";
  question.textContent = inputData.question;
  container.appendChild(question);

  // Textarea container
  var textareaContainer = document.createElement("div");
  textareaContainer.className = "text-input-container";
  textareaContainer.style.marginTop = "var(--space-md, 0.75rem)";

  // Textarea input
  var textarea = document.createElement("textarea");
  textarea.className = "text-input";
  textarea.placeholder = inputData.placeholder || "Enter your response...";
  textarea.rows = inputData.rows || 4;
  textarea.dataset.semanticLabel = inputData.semantic_label || "";

  textareaContainer.appendChild(textarea);
  container.appendChild(textareaContainer);

  // In batch mode, skip individual submit button and pending lock
  if (!inputData._batch) {
    var submitBtn = document.createElement("button");
    submitBtn.className = "btn btn--primary";
    submitBtn.textContent = "Cue";
    submitBtn.style.marginTop = "var(--space-md, 0.75rem)";
    submitBtn.addEventListener("click", function(e) {
      e.stopPropagation();
      var thermal = container.dataset.thermal ? JSON.parse(container.dataset.thermal) : null;
      handleTextResponse(textarea.value, textarea, submitBtn, inputData.semantic_label, inputData.question, thermal);
    });
    container.appendChild(submitBtn);
    setPendingInput(true);
  }

  container.appendChild(createModifierIcon());

  return container;
}

// Handle text input response
function handleTextResponse(value, textarea, submitBtn, semanticLabel, question, thermal) {
  playSound("affirmative");
  console.log("Text input response:", value, "Label:", semanticLabel);

  // Send the response with question context
  var response;
  if (semanticLabel) {
    response = semanticLabel + ": " + value;
  } else if (question) {
    response = "[Response to \"" + question + "\"]: " + value;
  } else {
    response = value;
  }

  // Emit as input_response so backend creates a token
  var inputPayload = {
    input: {
      key: semanticLabel || question || "response",
      value: value,
      question: question || ""
    }
  };
  if (thermal) {
    inputPayload.input.thermal = thermal;
  }
  socket.emit("input_response", inputPayload);

  // Disable textarea
  textarea.disabled = true;
  textarea.classList.add("disabled");

  // Remove submit button
  submitBtn.remove();

  // Add submitted text indicator (truncated at 2 lines)
  var choiceIndicator = document.createElement("div");
  choiceIndicator.className = "choice-indicator choice-indicator--text";

  var labelEl = document.createElement("strong");
  labelEl.textContent = "Submitted: ";
  choiceIndicator.appendChild(labelEl);

  var textSpan = document.createElement("span");
  textSpan.className = "submitted-text";
  textSpan.textContent = value;
  choiceIndicator.appendChild(textSpan);

  textarea.parentNode.parentNode.appendChild(choiceIndicator);

  // Unblock input
  setPendingInput(false);

  // Add user message immediately
  addMessage("user", response);
}

// Create approval gate
function createApprovalGate(approvalData) {
  const container = document.createElement('div');
  container.className = 'structured-question approval-gate';

  // Title showing action and description
  const title = document.createElement('p');
  title.className = 'card__description approval-gate__title';
  const actionText = approvalData.action || 'Action';
  const descText = approvalData.description || 'Approve this action';
  title.innerHTML = `<strong>${actionText}:</strong> ${descText}`;
  container.appendChild(title);

  // Target (if provided)
  if (approvalData.target) {
    const target = document.createElement('p');
    target.className = 'card__description approval-gate__target';
    target.textContent = `Target: ${approvalData.target}`;
    target.style.fontSize = 'var(--font-size-sm, 0.875rem)';
    target.style.color = 'var(--text-secondary, #888)';
    target.style.marginTop = 'var(--space-xs, 0.25rem)';
    container.appendChild(target);
  }

  // Preview (if provided) - truncated to 5 lines
  if (approvalData.preview) {
    const previewContainer = document.createElement('div');
    previewContainer.className = 'approval-gate__preview';
    previewContainer.style.marginTop = 'var(--space-md, 0.75rem)';
    previewContainer.style.padding = 'var(--space-md, 0.75rem)';
    previewContainer.style.background = 'var(--surface-secondary, rgba(255, 255, 255, 0.03))';
    previewContainer.style.borderRadius = 'var(--radius-sm, 4px)';
    previewContainer.style.fontSize = 'var(--font-size-sm, 0.875rem)';
    previewContainer.style.maxHeight = '120px';
    previewContainer.style.overflow = 'hidden';
    previewContainer.style.position = 'relative';

    const preview = document.createElement('pre');
    preview.className = 'approval-gate__preview-text';
    preview.style.margin = '0';
    preview.style.whiteSpace = 'pre-wrap';
    preview.style.wordWrap = 'break-word';
    preview.style.fontFamily = 'monospace';
    preview.textContent = approvalData.preview;
    previewContainer.appendChild(preview);

    container.appendChild(previewContainer);
  }

  // Button group
  const buttonGroup = document.createElement('div');
  buttonGroup.className = 'button-group';
  buttonGroup.style.marginTop = 'var(--space-md, 0.75rem)';
  buttonGroup.style.display = 'flex';
  buttonGroup.style.gap = 'var(--space-sm, 0.5rem)';

  const approveBtn = document.createElement('button');
  approveBtn.className = 'btn btn--primary';
  approveBtn.textContent = 'Approve';
  approveBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    handleApprovalResponse('Approve', buttonGroup, approvalData);
  });

  const rejectBtn = document.createElement('button');
  rejectBtn.className = 'btn btn--secondary';
  rejectBtn.textContent = 'Reject';
  rejectBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    handleApprovalResponse('Reject', buttonGroup, approvalData);
  });

  buttonGroup.appendChild(approveBtn);
  buttonGroup.appendChild(rejectBtn);
  container.appendChild(buttonGroup);

  container.appendChild(createModifierIcon());

  // Block other input when approval is pending
  setPendingInput(true);

  return container;
}

// Handle approval response
function handleApprovalResponse(decision, buttonGroup, approvalData) {
  playSound(decision === "Approve" ? "affirmative" : "negatory");
  console.log("Approval response:", decision, "Data:", approvalData);

  // Send as approval_response so backend handles token creation and context
  socket.emit("approval_response", {
    decision: decision,
    approval_data: approvalData
  });

  // Disable all buttons
  const buttons = buttonGroup.querySelectorAll('button');
  buttons.forEach(btn => {
    btn.disabled = true;
    btn.style.opacity = '0.5';
  });

  // Highlight selected button
  const selectedBtn = Array.from(buttons).find(btn => btn.textContent === decision);
  if (selectedBtn) {
    selectedBtn.style.opacity = '1';
    selectedBtn.style.fontWeight = 'bold';
  }

  // Add choice indicator
  const choiceIndicator = document.createElement('div');
  choiceIndicator.className = 'choice-indicator';
  choiceIndicator.style.marginTop = 'var(--space-sm, 0.5rem)';
  choiceIndicator.innerHTML = `<strong>Decision:</strong> ${decision}`;
  buttonGroup.parentNode.appendChild(choiceIndicator);

  // Unblock input
  setPendingInput(false);

  // Add user message immediately
  addMessage("user", decision);
}

// Set pending input state
function setPendingInput(pending) {
  hasPendingInput = pending;

  // Update input field state
  if (drawerTextInput) {
    drawerTextInput.disabled = pending;
  }

  if (drawerSendButton) {
    drawerSendButton.disabled = pending;
  }

  // Update drawer toggle indicator
  if (drawerToggle) {
    if (pending) {
      drawerToggle.classList.add('has-pending-input');
    } else {
      drawerToggle.classList.remove('has-pending-input');
    }
  }
}

// Send text response
function sendTextResponse(text) {
  addMessage("user", text);
  socket.emit("text_message", { text: text });
}

// ============================================
// Document Editor Widget (Phase 2B)
// ============================================

// Track open document editors by ID
var openDocuments = {};

function createDocumentEditor(docData) {
  var docId = docData.id;
  var action = docData.action || "open";

  // Handle close action
  if (action === "close" && openDocuments[docId]) {
    var existing = openDocuments[docId];
    existing.classList.add("document-editor--closed");
    delete openDocuments[docId];
    socket.emit("document_update", { id: docId, action: "close" });
    return document.createComment("document closed: " + docId);
  }

  // Handle update action - update existing editor
  if (action === "update" && openDocuments[docId]) {
    var editor = openDocuments[docId];
    var contentArea = editor.querySelector(".document-editor__content");
    if (contentArea && docData.content) {
      contentArea.value = docData.content;
    }
    var versionEl = editor.querySelector(".document-editor__version");
    if (versionEl && docData.version) {
      versionEl.textContent = "v" + docData.version;
    }
    return document.createComment("document updated: " + docId);
  }

  // Create new editor
  var article = document.createElement("article");
  article.className = "document-editor";
  article.dataset.docId = docId;

  // Header
  var header = document.createElement("header");
  header.className = "document-editor__header";

  var title = document.createElement("h3");
  title.className = "document-editor__title";
  title.textContent = docData.title || "Untitled Document";

  var version = document.createElement("span");
  version.className = "document-editor__version";
  version.textContent = "v" + (docData.version || 1);

  header.appendChild(title);
  header.appendChild(version);
  article.appendChild(header);

  // Content area (editable textarea)
  var contentSection = document.createElement("section");
  contentSection.className = "document-editor__body";

  var textarea = document.createElement("textarea");
  textarea.className = "document-editor__content";
  textarea.value = docData.content || "";
  textarea.rows = 12;
  textarea.placeholder = "Document content...";

  contentSection.appendChild(textarea);
  article.appendChild(contentSection);

  // Footer with actions
  var footer = document.createElement("footer");
  footer.className = "document-editor__footer";

  var saveBtn = document.createElement("button");
  saveBtn.className = "btn btn--primary";
  saveBtn.textContent = "Save";
  saveBtn.addEventListener("click", function(e) {
    e.stopPropagation();
    socket.emit("document_update", {
      id: docId,
      action: "update",
      content: textarea.value,
      editor: "user"
    });
    // Update version display optimistically
    var currentV = parseInt(version.textContent.replace("v", "")) || 1;
    version.textContent = "v" + (currentV + 1);
  });

  var closeBtn = document.createElement("button");
  closeBtn.className = "btn btn--secondary";
  closeBtn.textContent = "Close";
  closeBtn.addEventListener("click", function(e) {
    e.stopPropagation();
    socket.emit("document_update", { id: docId, action: "close" });
    article.classList.add("document-editor--closed");
    delete openDocuments[docId];
  });

  footer.appendChild(saveBtn);
  footer.appendChild(closeBtn);
  article.appendChild(footer);

  // Track open editor
  openDocuments[docId] = article;

  return article;
}

// Listen for server-side document updates
socket.on("document_update", function(data) {
  var docId = data.id;
  if (openDocuments[docId]) {
    var editor = openDocuments[docId];
    if (data.content !== undefined) {
      var contentArea = editor.querySelector(".document-editor__content");
      if (contentArea) {
        contentArea.value = data.content;
      }
    }
    if (data.version !== undefined) {
      var versionEl = editor.querySelector(".document-editor__version");
      if (versionEl) {
        versionEl.textContent = "v" + data.version;
      }
    }
    if (data.action === "close") {
      editor.classList.add("document-editor--closed");
      delete openDocuments[docId];
    }
  }
});

// ============================================
// CUE Card Widget (Phase 3B)
// ============================================

function createCueCard(cueData) {
  var container = document.createElement("div");
  container.className = "structured-question cue-card";

  // Tool name header
  var title = document.createElement("p");
  title.className = "card__description cue-card__title";
  title.innerHTML = "<strong>CUE:</strong> " + (cueData.tool || "unknown");
  container.appendChild(title);

  // Cue ID
  if (cueData.cue_id) {
    var cueId = document.createElement("p");
    cueId.className = "card__description cue-card__id";
    cueId.textContent = cueData.cue_id;
    cueId.style.fontSize = "var(--font-size-sm, 0.875rem)";
    cueId.style.color = "var(--text-secondary, #888)";
    container.appendChild(cueId);
  }

  // Payload preview
  if (cueData.payload) {
    var previewContainer = document.createElement("div");
    previewContainer.className = "cue-card__preview";

    var preview = document.createElement("pre");
    preview.className = "cue-card__preview-text";
    preview.textContent = JSON.stringify(cueData.payload, null, 2);
    previewContainer.appendChild(preview);

    container.appendChild(previewContainer);
  }

  // Approve/Reject buttons
  var buttonGroup = document.createElement("div");
  buttonGroup.className = "button-group";
  buttonGroup.style.marginTop = "var(--space-md, 0.75rem)";
  buttonGroup.style.display = "flex";
  buttonGroup.style.gap = "var(--space-sm, 0.5rem)";

  var approveBtn = document.createElement("button");
  approveBtn.className = "btn btn--primary";
  approveBtn.textContent = "Approve";
  approveBtn.addEventListener("click", function(e) {
    e.stopPropagation();
    socket.emit("cue_dispatch", {
      cue_id: cueData.cue_id,
      tool: cueData.tool,
      payload: cueData.payload,
      decision: "approved"
    });
    handleCueResponse("Approved", buttonGroup, cueData);
  });

  var rejectBtn = document.createElement("button");
  rejectBtn.className = "btn btn--secondary";
  rejectBtn.textContent = "Reject";
  rejectBtn.addEventListener("click", function(e) {
    e.stopPropagation();
    handleCueResponse("Rejected", buttonGroup, cueData);
  });

  buttonGroup.appendChild(approveBtn);
  buttonGroup.appendChild(rejectBtn);
  container.appendChild(buttonGroup);

  container.appendChild(createModifierIcon());

  setPendingInput(true);

  return container;
}

function handleCueResponse(decision, buttonGroup, cueData) {
  var buttons = buttonGroup.querySelectorAll("button");
  buttons.forEach(function(btn) {
    btn.disabled = true;
    btn.style.opacity = "0.5";
  });

  var selectedBtn = Array.from(buttons).find(function(btn) {
    return btn.textContent === decision || btn.textContent === "Approve" && decision === "Approved"
           || btn.textContent === "Reject" && decision === "Rejected";
  });
  if (selectedBtn) {
    selectedBtn.style.opacity = "1";
    selectedBtn.style.fontWeight = "bold";
  }

  var indicator = document.createElement("div");
  indicator.className = "choice-indicator";
  indicator.innerHTML = "<strong>Decision:</strong> " + decision;
  buttonGroup.parentNode.appendChild(indicator);

  setPendingInput(false);

  var response = "[CUE " + decision + ": " + (cueData.tool || "") + " " + (cueData.cue_id || "") + "]";
  addMessage("user", response);
  socket.emit("text_message", { text: response });
}

// Update relative timestamps
function updateTimestamps() {
  const messages = document.querySelectorAll('.conversation .card[data-timestamp]');
  messages.forEach(message => {
    const timestamp = parseInt(message.dataset.timestamp);
    const timestampEl = message.querySelector('.message-timestamp');
    if (timestampEl) {
      timestampEl.textContent = getRelativeTime(timestamp);
    }
  });
}

function getRelativeTime(timestamp) {
  const seconds = Math.floor((Date.now() - timestamp) / 1000);
  if (seconds < 10) return 'just now';
  if (seconds < 60) return `${seconds}s ago`;

  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;

  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;

  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

// Update timestamps every 10 seconds
setInterval(updateTimestamps, 10000);

function addSystemMessage(text) {
  const messageCard = document.createElement('article');
  messageCard.className = 'card';

  const body = document.createElement('div');
  body.className = 'card__body';

  const description = document.createElement('p');
  description.className = 'card__description';
  description.style.color = 'var(--text-secondary)';
  description.textContent = text;

  body.appendChild(description);
  messageCard.appendChild(body);

  conversation.appendChild(messageCard);
  conversation.scrollTop = conversation.scrollHeight;
}

// ============================================
// Modifier Tokens - Token Created Listener
// ============================================

socket.on("token_created", function(data) {
  console.log("Token created:", data.token_id, data.type);

  // Store in registry (keep all fields for hex log display)
  tokenRegistry[data.token_id] = {
    token_id: data.token_id,
    type: data.type,
    label: data.label,
    value: data.value,
    question: data.question || "",
    slider_value: data.slider_value,
    key: data.key,
    tags: data.tags || [],
    temperature: data.temperature,
    base_temp: data.base_temp,
    cooling_rate: data.cooling_rate,
    visibility: data.visibility,
    created_at: data.created_at,
    last_accessed: data.last_accessed || data.created_at
  };

  // Gallery tokens: map IDs, store images, create modifier thumbnail, start thermal clock
  if (data.type === "gallery" && data.gallery_id) {
    galleryTokenMap[data.gallery_id] = data.token_id;
    tokenGalleryMap[data.token_id] = data.gallery_id;
    // Store images in registry for lightbox rehydration
    tokenRegistry[data.token_id].images = data.images || [];
    tokenRegistry[data.token_id].title = data.title || "Gallery";
    tokenRegistry[data.token_id].image_count = data.image_count || 0;
    // Seed galleryRegistry so lightbox works
    if (!galleryRegistry[data.gallery_id]) {
      galleryRegistry[data.gallery_id] = { images: data.images || [], title: data.title || "" };
    }
    // Create the modifier thumbnail (purple dot + thermal countdown)
    createModifierThumbnail(data.token_id, null);
    // Emit create_modifier to start the thermal clock
    socket.emit("create_modifier", { token_id: data.token_id });
    // Track in pinnedGalleries for pin-icon state
    pinnedGalleries[data.gallery_id] = modifierTokens[data.token_id];
    addCueCardToStream(data.token_id);
    return;
  }

  // Find the most recent untagged structured-question in the conversation
  var questions = conversation.querySelectorAll(".structured-question:not([data-token-id])");
  if (questions.length > 0) {
    var lastQuestion = questions[questions.length - 1];
    lastQuestion.setAttribute("data-token-id", data.token_id);
    lastQuestion.setAttribute("data-token-type", data.type);
    lastQuestion.setAttribute("data-token-label", data.label);
    lastQuestion.setAttribute("data-token-value", data.value);

    // Show and wire up the modifier icon
    var modIcon = lastQuestion.querySelector(".pin-icon");
    if (modIcon) {
      modIcon.dataset.tokenId = data.token_id;
      modIcon.setAttribute("data-state", "inactive");
      modIcon.style.display = "flex";
    }
  }

  // Add to activity stream
  addCueCardToStream(data.token_id);
});

// ============================================
// Modifier Tokens - Thumbnail Management
// ============================================

function createModifierThumbnail(tokenId, sourceCard) {
  var container = document.getElementById("pinnedTokens");
  if (!container) return;

  // Avoid duplicates
  if (container.querySelector("[data-token-id=\"" + tokenId + "\"]")) return;

  var data = tokenRegistry[tokenId];
  if (!data) return;

  var thumb = document.createElement("div");
  thumb.className = "pinned-thumbnail";
  thumb.setAttribute("data-token-id", tokenId);

  var dot = document.createElement("span");
  dot.className = "pinned-thumbnail__dot";
  dot.setAttribute("data-type", data.type);
  thumb.appendChild(dot);

  var info = document.createElement("div");
  info.className = "pinned-thumbnail__info";

  var label = document.createElement("p");
  label.className = "pinned-thumbnail__label";
  var formattedLabel = formatTokenLabel(data);
  label.textContent = formattedLabel;
  info.appendChild(label);

  var value = document.createElement("p");
  value.className = "pinned-thumbnail__value";
  // Skip value if the label already shows it (avoids double-labeling)
  var rawValue = data.value || "";
  value.textContent = (rawValue === formattedLabel) ? "" : rawValue;
  info.appendChild(value);

  // Countdown timer
  var countdown = document.createElement("p");
  countdown.className = "pinned-thumbnail__countdown";
  countdown.textContent = "";
  info.appendChild(countdown);

  thumb.appendChild(info);

  // Click countdown = mint fresh modifier (renew), click elsewhere = lightbox
  thumb.addEventListener("click", function(e) {
    if (e.target === countdown || e.target.classList.contains("pinned-thumbnail__countdown")) {
      e.stopPropagation();
      socket.emit("create_modifier", { token_id: tokenId });
      return;
    }
    // Gallery tokens open the gallery lightbox instead of the token lightbox
    var gId = tokenGalleryMap[tokenId];
    if (data && data.type === "gallery" && gId) {
      openGalleryLightbox(gId, 0);
      return;
    }
    openLightbox(tokenId);
  });

  // Most recent goes to the top
  if (container.firstChild) {
    container.insertBefore(thumb, container.firstChild);
  } else {
    container.appendChild(thumb);
  }
  modifierTokens[tokenId] = thumb;

  // Start thermal countdown based on modifier heat
  startThermalCountdown(tokenId);
  updateClearAllVisibility();
  if (window._updateStreamPosition) window._updateStreamPosition();
}

// Sort pinned thumbnails by expiry (soonest-to-expire at top) with FLIP animation
function promoteModifierThumbnail(tokenId) {
  var container = document.getElementById("pinnedTokens");
  if (!container) return;

  // Gather all pinned thumbnails with their remaining time
  var thumbs = Array.prototype.slice.call(container.querySelectorAll(".pinned-thumbnail"));
  if (thumbs.length < 2) return;

  // FLIP: capture old positions
  var firstRects = {};
  for (var i = 0; i < thumbs.length; i++) {
    var id = thumbs[i].getAttribute("data-token-id");
    if (id) firstRects[id] = thumbs[i].getBoundingClientRect();
  }

  // Sort by time remaining (ascending -- soonest expiry first)
  thumbs.sort(function(a, b) {
    return getTimeRemaining(a.getAttribute("data-token-id"))
         - getTimeRemaining(b.getAttribute("data-token-id"));
  });

  // Re-insert in sorted order (after clear-all link)
  var clearAll = container.querySelector(".pinned-clear-all");
  for (var j = 0; j < thumbs.length; j++) {
    if (clearAll && clearAll.nextSibling) {
      container.insertBefore(thumbs[j], clearAll.nextSibling);
    } else {
      container.appendChild(thumbs[j]);
    }
  }

  // FLIP: compute deltas and animate
  for (var k = 0; k < thumbs.length; k++) {
    var kid = thumbs[k].getAttribute("data-token-id");
    if (!kid || !firstRects[kid]) continue;
    var lastRect = thumbs[k].getBoundingClientRect();
    var dy = firstRects[kid].top - lastRect.top;
    if (dy === 0) continue;
    thumbs[k].style.transform = "translateY(" + dy + "px)";
    thumbs[k].style.transition = "none";
  }
  container.offsetHeight;
  for (var m = 0; m < thumbs.length; m++) {
    thumbs[m].style.transition = "";
    thumbs[m].style.transform = "";
  }
}

// Calculate hours remaining until a pinned token reaches 0 degrees
function getTimeRemaining(tokenId) {
  if (!tokenId) return Infinity;
  var thermal = modifierThermalData[tokenId];
  var data = tokenRegistry[tokenId];
  var storedTemp, coolingRate, refStr;

  if (thermal) {
    storedTemp = thermal.temperature || thermal.base_temp || 60;
    coolingRate = thermal.cooling_rate || 30.0;
    refStr = thermal.created_at;
  } else if (data) {
    storedTemp = data.temperature || data.base_temp || 75;
    coolingRate = data.cooling_rate || 5.0;
    refStr = data.last_accessed || data.created_at;
  } else {
    return Infinity;
  }

  var refMs = refStr ? new Date(refStr).getTime() : Date.now();
  var hoursElapsed = (Date.now() - refMs) / 3600000;
  var currentTemp = storedTemp - (coolingRate * hoursElapsed);
  if (currentTemp <= 0) return 0;
  return currentTemp / coolingRate;
}

// Thermal countdown tracking
var modifierCountdownIntervals = {};

// Visibility -> cooling modifier (mirrors cue-mem config.yaml)
var COOLING_MODIFIERS = {
  global: 0.5,
  shared: 1.0,
  private: 2.0,
  local: 1.5
};

function startThermalCountdown(tokenId) {
  // Clear any existing interval
  if (modifierCountdownIntervals[tokenId]) {
    clearInterval(modifierCountdownIntervals[tokenId]);
  }

  // Use modifier thermal data if available (ephemeral profile: 60deg, 10deg/hr)
  var thermal = modifierThermalData[tokenId];
  var storedTemp, coolingRate, refStr;

  if (thermal) {
    storedTemp = thermal.temperature || thermal.base_temp || 60;
    coolingRate = thermal.cooling_rate || 30.0;
    refStr = thermal.created_at;
  } else {
    // Fallback to target token data
    var data = tokenRegistry[tokenId];
    if (!data) return;
    storedTemp = data.temperature || data.base_temp || 75;
    coolingRate = data.cooling_rate || 5.0;
    refStr = data.last_accessed || data.created_at;
  }

  // Modifier tokens use shared visibility (cooling modifier = 1.0)
  var effectiveRate = coolingRate * 1.0;

  var refMs = refStr ? new Date(refStr).getTime() : Date.now();

  function updateCountdown() {
    var thumb = modifierTokens[tokenId];
    if (!thumb) {
      clearInterval(modifierCountdownIntervals[tokenId]);
      delete modifierCountdownIntervals[tokenId];
      return;
    }

    var countdownEl = thumb.querySelector(".pinned-thumbnail__countdown");
    if (!countdownEl) return;

    // Calculate current temperature from thermal decay
    var hoursElapsed = (Date.now() - refMs) / 3600000;
    var currentTemp = storedTemp - (effectiveRate * hoursElapsed);
    currentTemp = Math.max(0, Math.min(100, currentTemp));

    if (currentTemp <= 0) {
      // Frozen -- remove thumbnail
      removeModifierThumbnail(tokenId);
      clearInterval(modifierCountdownIntervals[tokenId]);
      delete modifierCountdownIntervals[tokenId];
      var card = conversation.querySelector("[data-token-id=\"" + tokenId + "\"]");
      if (card) {
        var modIcon = card.querySelector(".pin-icon");
        if (modIcon) modIcon.setAttribute("data-state", "inactive");
      }
      return;
    }

    // Time remaining until freeze (0 degrees)
    var hoursRemaining = currentTemp / effectiveRate;
    var totalMins = Math.floor(hoursRemaining * 60);
    var hours = Math.floor(totalMins / 60);
    var mins = totalMins % 60;

    if (hours > 0) {
      countdownEl.textContent = hours + "h " + mins + "m";
    } else {
      countdownEl.textContent = mins + "m";
    }

    // Visual warning when cold (below 25 degrees)
    if (currentTemp < 25) {
      thumb.style.opacity = "0.5";
      countdownEl.style.color = "var(--state-recording, #ff4444)";
    } else {
      thumb.style.opacity = "";
      countdownEl.style.color = "";
    }
  }

  updateCountdown();
  modifierCountdownIntervals[tokenId] = setInterval(updateCountdown, 30000);
}

function removeModifierThumbnail(tokenId) {
  var thumb = modifierTokens[tokenId];
  if (thumb && thumb.parentNode) {
    thumb.parentNode.removeChild(thumb);
  }
  delete modifierTokens[tokenId];
  delete modifierThermalData[tokenId];
  delete modifiersByTarget[tokenId];
  // Clean up countdown interval
  if (modifierCountdownIntervals[tokenId]) {
    clearInterval(modifierCountdownIntervals[tokenId]);
    delete modifierCountdownIntervals[tokenId];
  }
  updateClearAllVisibility();
  if (window._updateStreamPosition) window._updateStreamPosition();
}

var PINNED_VISIBLE_MAX = 3;

function updateClearAllVisibility() {
  var btn = document.getElementById("pinnedClearAll");
  if (!btn) return;
  var hasPins = Object.keys(modifierTokens).length > 0 || Object.keys(pinnedGalleries).length > 0;
  btn.style.display = hasPins ? "block" : "";
  updatePinnedTruncation();
}

function updatePinnedTruncation() {
  var container = document.getElementById("pinnedTokens");
  if (!container) return;
  var thumbs = container.querySelectorAll(".pinned-thumbnail");
  var overflow = thumbs.length - PINNED_VISIBLE_MAX;

  // Show/hide thumbnails beyond max
  for (var i = 0; i < thumbs.length; i++) {
    thumbs[i].style.display = i < PINNED_VISIBLE_MAX ? "" : "none";
  }

  // Manage overflow badge
  var badge = container.querySelector(".pinned-overflow");
  if (overflow > 0) {
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "pinned-overflow";
      container.appendChild(badge);
    }
    badge.textContent = "+" + overflow + " more";
    badge.style.display = "";
  } else if (badge) {
    badge.style.display = "none";
  }
}

(function() {
  var clearBtn = document.getElementById("pinnedClearAll");
  if (!clearBtn) return;
  clearBtn.addEventListener("click", function() {
    var ids = Object.keys(modifierTokens);
    for (var i = 0; i < ids.length; i++) {
      socket.emit("remove_modifier", { token_id: ids[i] });
      removeModifierThumbnail(ids[i]);
      addCueCardToStream(ids[i]);
      var card = conversation.querySelector("[data-token-id=\"" + ids[i] + "\"]");
      if (card) {
        var modIcon = card.querySelector(".pin-icon");
        if (modIcon) modIcon.setAttribute("data-state", "inactive");
      }
    }
    // Also clear pinned galleries (emit remove_modifier for backend-tracked ones)
    var galleryIds = Object.keys(galleryTokenMap);
    for (var j = 0; j < galleryIds.length; j++) {
      var gTokId = galleryTokenMap[galleryIds[j]];
      if (gTokId) {
        socket.emit("remove_modifier", { token_id: gTokId });
        removeModifierThumbnail(gTokId);
        delete tokenGalleryMap[gTokId];
      }
      delete galleryTokenMap[galleryIds[j]];
      delete pinnedGalleries[galleryIds[j]];
      // Reset pin icon on the gallery strip
      var strip = document.querySelector("[data-gallery-id='" + galleryIds[j] + "'].gallery-strip");
      if (strip) {
        var pinIcon = strip.querySelector(".pin-icon");
        if (pinIcon) pinIcon.setAttribute("data-state", "inactive");
      }
    }
    // Also clear any legacy local-only pinned galleries
    var legacyGalleryIds = Object.keys(pinnedGalleries);
    for (var k = 0; k < legacyGalleryIds.length; k++) {
      unpinGallery(legacyGalleryIds[k]);
    }
    clearBtn.style.display = "none";
  });
})();

// ============================================
// Modifier Tokens - Lightbox Modal
// ============================================

function openLightbox(tokenId) {
  var data = tokenRegistry[tokenId];
  if (!data) return;

  currentLightboxTokenId = tokenId;

  var lightbox = document.getElementById("lightbox");
  var title = document.getElementById("lightboxTitle");
  var body = document.getElementById("lightboxBody");

  title.textContent = formatTokenLabel(data);
  renderLightboxBody(body, data, false);

  // Show view mode buttons, hide edit mode buttons
  var editableTypes = ["scalar_param", "text_input", "yes_no_response", "approval_response"];
  var rawKey = data.label || data.key || "";
  var isSystemToken = rawKey.indexOf("cuesheet_session_") === 0
    || rawKey.indexOf("buff_cuesheet_") === 0
    || rawKey.indexOf("mod_") === 0;
  var isEditable = editableTypes.indexOf(data.type) !== -1 && !isSystemToken;
  document.getElementById("lightboxEditBtn").style.display = isEditable ? "" : "none";
  document.getElementById("lightboxUnpinBtn").style.display = "";
  document.getElementById("lightboxSaveBtn").style.display = "none";
  document.getElementById("lightboxCancelBtn").style.display = "none";

  lightbox.style.display = "flex";
}

function closeLightbox() {
  var lightbox = document.getElementById("lightbox");
  lightbox.style.display = "none";
  currentLightboxTokenId = null;
}

function renderLightboxBody(container, data, editable) {
  container.innerHTML = "";

  // Question text
  if (data.question) {
    var questionEl = document.createElement("p");
    questionEl.className = "lightbox__question";
    questionEl.textContent = data.question;
    container.appendChild(questionEl);
  }

  if (data.type === "yes_no_response" || data.type === "approval_response" || data.type === "cue_dispatch") {
    // Pick labels based on token type
    var isApprovalType = (data.type === "approval_response" || data.type === "cue_dispatch");
    var positiveLabel = isApprovalType ? "Approve" : "Yes";
    var negativeLabel = isApprovalType ? "Reject" : "No";
    var positiveValue = isApprovalType ? "Approve" : "Yes";
    var negativeValue = isApprovalType ? "Reject" : "No";
    // Normalize current value for matching
    var isPositive = (data.value === "Yes" || data.value === "Approve" || data.value === "Approved");

    if (editable) {
      var btnGroup = document.createElement("div");
      btnGroup.className = "button-group";
      btnGroup.style.display = "flex";
      btnGroup.style.gap = "var(--space-sm, 0.5rem)";

      var yesBtn = document.createElement("button");
      yesBtn.className = "btn " + (isPositive ? "btn--primary" : "btn--secondary");
      yesBtn.textContent = positiveLabel;
      yesBtn.addEventListener("click", function() {
        container.dataset.newValue = positiveValue;
        yesBtn.className = "btn btn--primary";
        noBtn.className = "btn btn--secondary";
      });

      var noBtn = document.createElement("button");
      noBtn.className = "btn " + (!isPositive ? "btn--primary" : "btn--secondary");
      noBtn.textContent = negativeLabel;
      noBtn.addEventListener("click", function() {
        container.dataset.newValue = negativeValue;
        noBtn.className = "btn btn--primary";
        yesBtn.className = "btn btn--secondary";
      });

      btnGroup.appendChild(yesBtn);
      btnGroup.appendChild(noBtn);
      container.appendChild(btnGroup);
      container.dataset.newValue = data.value;
    } else {
      var valueEl = document.createElement("p");
      valueEl.className = "card__description";
      valueEl.style.fontWeight = "var(--font-weight-bold, 700)";
      var answerPrefix = isApprovalType ? "Decision" : "Answer";
      valueEl.textContent = answerPrefix + ": " + data.value;
      container.appendChild(valueEl);
    }
  } else if (data.type === "scalar_param") {
    if (editable) {
      var sliderEl = document.createElement("input");
      sliderEl.type = "range";
      sliderEl.min = 0;
      sliderEl.max = 100;
      sliderEl.step = 1;
      sliderEl.value = data.slider_value || 50;
      sliderEl.className = "slider-input";
      sliderEl.style.width = "100%";

      var sliderValDisplay = document.createElement("div");
      sliderValDisplay.className = "slider-value";
      sliderValDisplay.textContent = sliderEl.value + "%";

      sliderEl.addEventListener("input", function() {
        sliderValDisplay.textContent = sliderEl.value + "%";
        container.dataset.newValue = sliderEl.value;
      });

      container.appendChild(sliderEl);
      container.appendChild(sliderValDisplay);
      container.dataset.newValue = String(data.slider_value || 50);
    } else {
      var valueEl = document.createElement("p");
      valueEl.className = "card__description";
      valueEl.style.fontWeight = "var(--font-weight-bold, 700)";
      valueEl.textContent = "Value: " + data.value + " (" + (data.slider_value || "") + "%)";
      container.appendChild(valueEl);
    }
  } else if (data.type === "text_input") {
    if (editable) {
      var textareaEl = document.createElement("textarea");
      textareaEl.className = "text-input";
      textareaEl.value = data.value || "";
      textareaEl.rows = 6;
      textareaEl.addEventListener("input", function() {
        container.dataset.newValue = textareaEl.value;
      });
      container.appendChild(textareaEl);
      container.dataset.newValue = data.value || "";
    } else {
      renderMarkdown(container, data.value || "");
    }
  }

  // Hex log box (view mode only)
  if (!editable) {
    container.appendChild(buildHexLog(data));
  }
}

// Build the small hex/token provenance box
function buildHexLog(data) {
  var box = document.createElement("details");
  box.className = "hex-log";

  var summary = document.createElement("summary");
  summary.className = "hex-log__summary";

  var summaryLabel = document.createTextNode("VRGB ");
  summary.appendChild(summaryLabel);

  var dot = document.createElement("span");
  dot.className = "hex-log__dot";
  dot.setAttribute("data-type", data.type || "");
  summary.appendChild(dot);

  box.appendChild(summary);

  var body = document.createElement("div");
  body.className = "hex-log__body";

  var lines = [];

  // Extract explicit hex from tags (scalar_param tokens)
  var hexTag = "";
  var sliderTag = "";
  var tags = data.tags || [];
  for (var i = 0; i < tags.length; i++) {
    if (tags[i].indexOf("hex:") === 0) hexTag = tags[i].substring(4);
    if (tags[i].indexOf("slider:") === 0) sliderTag = tags[i].substring(7);
  }

  // Resolve the hex -- either from VRGB encoding or thermal signature
  var hex, hsl, origin;

  if (data.type === "scalar_param" && hexTag && hexTag !== "#000000") {
    // Scalar with real VRGB encoding
    hex = hexTag;
    hsl = hexToHSL(hex);
    origin = "VRGB encoded from slider position " +
      (data.slider_value != null ? data.slider_value : sliderTag) + "/100";
  } else if (data.temperature != null) {
    // Derive from thermal signature -- immutable birth certificate
    var thermal = thermalToHex(
      data.temperature || 0,
      data.base_temp || 50,
      data.cooling_rate || 5
    );
    hex = thermal.hex;
    hsl = thermal.hsl;
    origin = thermalOriginLabel(data.temperature, data.base_temp, data.cooling_rate);
  } else {
    hex = null;
    hsl = null;
    origin = null;
  }

  // Coordinate display
  if (hex) {
    lines.push("coordinate  " + hex);
    lines.push("decoded     H " + hsl.h + "\u00b0 \u00b7 S " + hsl.s + "% \u00b7 L " + hsl.l + "%");
    lines.push("origin      " + origin);
  }

  // Token type context
  lines.push("");
  if (data.type === "scalar_param") {
    var sv = data.slider_value != null ? data.slider_value : sliderTag;
    lines.push("dimension   " + formatTokenLabel(data) + " @ " + sv + "/100");
  } else if (data.type === "yes_no_response" || data.type === "approval_response") {
    lines.push("gate        boolean (" + (data.value || "") + ")");
  } else if (data.type === "text_input") {
    lines.push("content     free-text semantic token");
  } else if (data.type === "cue_dispatch") {
    lines.push("action      dispatch token");
  }

  // Provenance
  lines.push("id          " + (data.token_id || "unknown"));
  if (data.temperature != null) {
    var thermalStr = data.temperature + "\u00b0";
    if (data.base_temp != null) thermalStr += " / base " + data.base_temp + "\u00b0";
    if (data.cooling_rate != null) thermalStr += " / -" + data.cooling_rate + "\u00b0/hr";
    lines.push("thermal     " + thermalStr);
  }
  if (data.visibility) {
    lines.push("visibility  " + data.visibility);
  }
  if (data.created_at) {
    lines.push("created     " + data.created_at.replace("T", " ").substring(0, 19));
  }
  if (modifiersByTarget[data.token_id] && modifiersByTarget[data.token_id].length > 0) {
    lines.push("modifiers   " + modifiersByTarget[data.token_id].length + " active");
  }

  body.textContent = lines.join("\n");
  box.appendChild(body);
  return box;
}

// Decode hex string to HSL components (for VRGB coordinate display)
function hexToHSL(hex) {
  hex = hex.replace("#", "");
  var r = parseInt(hex.substring(0, 2), 16) / 255;
  var g = parseInt(hex.substring(2, 4), 16) / 255;
  var b = parseInt(hex.substring(4, 6), 16) / 255;
  var max = Math.max(r, g, b);
  var min = Math.min(r, g, b);
  var h = 0, s = 0, l = (max + min) / 2;
  if (max !== min) {
    var d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
    else if (max === g) h = ((b - r) / d + 2) / 6;
    else h = ((r - g) / d + 4) / 6;
  }
  return {
    h: Math.round(h * 360),
    s: Math.round(s * 100),
    l: Math.round(l * 100)
  };
}

// Derive hex coordinate from thermal signature (immutable birth certificate)
// temperature → hue (hot=warm reds, cool=cold blues)
// base_temp → saturation (high base=vivid, low base=muted)
// cooling_rate → lightness (fast burn=bright, slow decay=dim)
function thermalToHex(temp, base, rate) {
  // H: temperature 0-100 → 240-0 (blue→red, hot temps = warm hues)
  var h = Math.round(240 - (temp / 100) * 240);
  // S: base_temp 0-100 → 20-90%
  var s = Math.round(20 + (base / 100) * 70);
  // L: cooling_rate 0-20 → 35-65% (fast=bright, slow=dim)
  var l = Math.round(35 + (Math.min(rate, 20) / 20) * 30);

  // HSL to hex
  var sn = s / 100, ln = l / 100;
  var c = (1 - Math.abs(2 * ln - 1)) * sn;
  var x = c * (1 - Math.abs((h / 60) % 2 - 1));
  var m = ln - c / 2;
  var r1 = 0, g1 = 0, b1 = 0;
  if (h < 60) { r1 = c; g1 = x; }
  else if (h < 120) { r1 = x; g1 = c; }
  else if (h < 180) { g1 = c; b1 = x; }
  else if (h < 240) { g1 = x; b1 = c; }
  else if (h < 300) { r1 = x; b1 = c; }
  else { r1 = c; b1 = x; }
  var ri = Math.round((r1 + m) * 255);
  var gi = Math.round((g1 + m) * 255);
  var bi = Math.round((b1 + m) * 255);
  var hex = "#" +
    ("0" + ri.toString(16)).slice(-2) +
    ("0" + gi.toString(16)).slice(-2) +
    ("0" + bi.toString(16)).slice(-2);
  return { hex: hex, hsl: { h: h, s: s, l: l } };
}

// Human-readable label for thermal provenance
function thermalOriginLabel(temp, base, rate) {
  var heat;
  if (temp >= 80) heat = "came in hot";
  else if (temp >= 60) heat = "warm arrival";
  else if (temp >= 40) heat = "steady presence";
  else if (temp >= 20) heat = "cool and persistent";
  else heat = "cold start";

  var burn;
  if (rate >= 10) burn = "fast burn";
  else if (rate >= 5) burn = "moderate decay";
  else if (rate >= 2) burn = "slow fade";
  else burn = "near-permanent";

  return heat + ", " + burn + " (" + temp + "\u00b0 @ -" + rate + "\u00b0/hr)";
}

// Lightbox button wiring
(function() {
  var editBtn = document.getElementById("lightboxEditBtn");
  var unpinBtn = document.getElementById("lightboxUnpinBtn");
  var saveBtn = document.getElementById("lightboxSaveBtn");
  var cancelBtn = document.getElementById("lightboxCancelBtn");
  var closeBtn = document.getElementById("lightboxClose");
  var backdrop = document.getElementById("lightboxBackdrop");

  unpinBtn.addEventListener("click", function() {
    if (!currentLightboxTokenId) return;
    var tokenId = currentLightboxTokenId;
    socket.emit("remove_modifier", { token_id: tokenId });
    removeModifierThumbnail(tokenId);
    // Drop back into activity stream
    addCueCardToStream(tokenId);
    // Reset modifier icon on the source card
    var card = conversation.querySelector("[data-token-id=\"" + tokenId + "\"]");
    if (card) {
      var modIcon = card.querySelector(".pin-icon");
      if (modIcon) {
        modIcon.setAttribute("data-state", "inactive");
      }
    }
    closeLightbox();
  });

  editBtn.addEventListener("click", function() {
    if (!currentLightboxTokenId) return;
    var data = tokenRegistry[currentLightboxTokenId];
    if (!data) return;
    var body = document.getElementById("lightboxBody");
    renderLightboxBody(body, data, true);
    editBtn.style.display = "none";
    unpinBtn.style.display = "none";
    saveBtn.style.display = "";
    cancelBtn.style.display = "";
  });

  cancelBtn.addEventListener("click", function() {
    if (!currentLightboxTokenId) return;
    var data = tokenRegistry[currentLightboxTokenId];
    if (!data) return;
    var body = document.getElementById("lightboxBody");
    renderLightboxBody(body, data, false);
    editBtn.style.display = "";
    unpinBtn.style.display = "";
    saveBtn.style.display = "none";
    cancelBtn.style.display = "none";
  });

  saveBtn.addEventListener("click", function() {
    if (!currentLightboxTokenId) return;
    var body = document.getElementById("lightboxBody");
    var newValue = body.dataset.newValue;
    socket.emit("edit_token", { token_id: currentLightboxTokenId, new_value: newValue });
    // Optimistic update
    tokenRegistry[currentLightboxTokenId].value = newValue;
    updateThumbnailValue(currentLightboxTokenId, newValue);
    promoteModifierThumbnail(currentLightboxTokenId);
    closeLightbox();
  });

  closeBtn.addEventListener("click", closeLightbox);
  backdrop.addEventListener("click", closeLightbox);
})();

function updateThumbnailValue(tokenId, newValue) {
  var thumb = modifierTokens[tokenId];
  if (!thumb) return;
  var valueEl = thumb.querySelector(".pinned-thumbnail__value");
  if (valueEl) {
    valueEl.textContent = newValue;
  }
}

// ============================================
// Modifier Tokens - Socket Confirmations
// ============================================

socket.on("modifier_created", function(data) {
  console.log("Modifier created for target:", data.token_id, "modifier:", data.modifier_id);
  var targetId = data.token_id;

  // Track modifier -> target mapping
  if (!modifiersByTarget[targetId]) {
    modifiersByTarget[targetId] = [];
  }
  modifiersByTarget[targetId].push(data.modifier_id);

  // Store modifier thermal data for countdown (fresh modifier = fresh countdown)
  modifierThermalData[targetId] = {
    temperature: data.temperature || 60,
    base_temp: data.base_temp || 60,
    cooling_rate: data.cooling_rate || 30.0,
    created_at: data.created_at || new Date().toISOString().replace("Z", "")
  };

  startThermalCountdown(targetId);
  promoteModifierThumbnail(targetId);
});

socket.on("modifiers_removed", function(data) {
  console.log("Modifiers removed for target:", data.token_id);
  delete modifiersByTarget[data.token_id];
  delete modifierThermalData[data.token_id];
  if (modifierCountdownIntervals[data.token_id]) {
    clearInterval(modifierCountdownIntervals[data.token_id]);
    delete modifierCountdownIntervals[data.token_id];
  }
});

socket.on("token_resolved", function(data) {
  console.log("Token resolved:", data.token_id, data.modifier_id || "(already)");
  removeModifierThumbnail(data.token_id);
  delete modifiersByTarget[data.token_id];
  delete modifierThermalData[data.token_id];
  if (modifierCountdownIntervals[data.token_id]) {
    clearInterval(modifierCountdownIntervals[data.token_id]);
    delete modifierCountdownIntervals[data.token_id];
  }
});

// Hydrate modifier targets on page load (most recent first)
socket.on("hydrate_modifiers", function(data) {
  var targets = data.modifiers || [];
  console.log("Hydrating %d modifier targets", targets.length);

  // Sort by last_accessed descending so most recent renders at top
  targets.sort(function(a, b) {
    var ta = a.last_accessed ? new Date(a.last_accessed).getTime() : 0;
    var tb = b.last_accessed ? new Date(b.last_accessed).getTime() : 0;
    return tb - ta;
  });

  for (var i = 0; i < targets.length; i++) {
    var target = targets[i];
    // Skip resolved items -- they stay in the chain but not in the pinned UI
    if (target.resolved) {
      if (!tokenRegistry[target.token_id]) {
        tokenRegistry[target.token_id] = target;
      }
      continue;
    }
    // Register in tokenRegistry if not already there
    if (!tokenRegistry[target.token_id]) {
      tokenRegistry[target.token_id] = target;
    }
    // Store modifier thermal data for countdown
    modifierThermalData[target.token_id] = {
      temperature: target.temperature,
      base_temp: target.base_temp,
      cooling_rate: target.cooling_rate,
      created_at: target.last_accessed || target.created_at
    };
    // Gallery rehydration: seed galleryRegistry so lightbox works after reload
    if (target.type === "gallery" && target.images) {
      var hydratedGalleryId = "hydrated_" + target.token_id;
      galleryRegistry[hydratedGalleryId] = { images: target.images, title: target.title || target.label || "Gallery" };
      galleryTokenMap[hydratedGalleryId] = target.token_id;
      tokenGalleryMap[target.token_id] = hydratedGalleryId;
      pinnedGalleries[hydratedGalleryId] = true;
    }
    // Create thumbnail with countdown
    createModifierThumbnail(target.token_id, null);
    // Also seed into activity stream
    addCueCardToStream(target.token_id);
  }
});

socket.on("token_updated", function(data) {
  console.log("Token updated confirmed:", data.token_id, data.new_value);
  if (tokenRegistry[data.token_id]) {
    tokenRegistry[data.token_id].value = data.new_value;
  }
  updateThumbnailValue(data.token_id, data.new_value);
  promoteModifierThumbnail(data.token_id);
});

socket.on("token_deleted", function(data) {
  console.log("Token deleted confirmed:", data.token_id);
  removeModifierThumbnail(data.token_id);
  removeCueCardFromStream(data.token_id);
  var card = conversation.querySelector("[data-token-id=\"" + data.token_id + "\"]");
  if (card) {
    var modIcon = card.querySelector(".pin-icon");
    if (modIcon) {
      modIcon.setAttribute("data-state", "sleeping");
    }
  }
  delete tokenRegistry[data.token_id];
});

// ============================================
// Attention Tracker (Page Visibility + Focus)
// ============================================

var AttentionTracker = (function() {
  var _running = false;
  var _startedAt = 0;
  var _tabAways = 0;
  var _hiddenStart = 0;
  var _totalHiddenMs = 0;
  var _focusLostCount = 0;
  var _activityCount = 0;
  var _lastActivity = 0;
  var _idleSamples = 0;
  var _idleTotal = 0;
  var _idleInterval = null;
  var _presenceDot = null;

  function _onVisibilityChange() {
    if (!_running) return;
    if (document.hidden) {
      _tabAways++;
      _hiddenStart = Date.now();
      _setPresence("away");
    } else {
      if (_hiddenStart > 0) {
        _totalHiddenMs += Date.now() - _hiddenStart;
        _hiddenStart = 0;
      }
      _setPresence("active");
    }
  }

  function _onBlur() {
    if (!_running) return;
    _focusLostCount++;
    _setPresence("away");
  }

  function _onFocus() {
    if (!_running) return;
    _setPresence("active");
  }

  function _onActivity() {
    if (!_running) return;
    _activityCount++;
    _lastActivity = Date.now();
  }

  function _setPresence(state) {
    if (_presenceDot) {
      _presenceDot.setAttribute("data-attention", state);
    }
  }

  function _sampleIdle() {
    if (!_running) return;
    _idleSamples++;
    var now = Date.now();
    // idle if no activity in last 3 seconds
    if (_lastActivity > 0 && (now - _lastActivity) > 3000) {
      _idleTotal++;
    }
  }

  return {
    start: function() {
      _running = true;
      _startedAt = Date.now();
      _tabAways = 0;
      _hiddenStart = 0;
      _totalHiddenMs = 0;
      _focusLostCount = 0;
      _activityCount = 0;
      _lastActivity = Date.now();
      _idleSamples = 0;
      _idleTotal = 0;
      _presenceDot = document.getElementById("challengePresence");

      document.addEventListener("visibilitychange", _onVisibilityChange);
      window.addEventListener("blur", _onBlur);
      window.addEventListener("focus", _onFocus);
      document.addEventListener("mousemove", _onActivity);
      document.addEventListener("touchstart", _onActivity);
      document.addEventListener("keydown", _onActivity);

      _idleInterval = setInterval(_sampleIdle, 1000);
      _setPresence(document.hidden ? "away" : "active");
    },

    stop: function() {
      _running = false;
      document.removeEventListener("visibilitychange", _onVisibilityChange);
      window.removeEventListener("blur", _onBlur);
      window.removeEventListener("focus", _onFocus);
      document.removeEventListener("mousemove", _onActivity);
      document.removeEventListener("touchstart", _onActivity);
      document.removeEventListener("keydown", _onActivity);
      if (_idleInterval) {
        clearInterval(_idleInterval);
        _idleInterval = null;
      }
      // close out any open hidden window
      if (_hiddenStart > 0) {
        _totalHiddenMs += Date.now() - _hiddenStart;
        _hiddenStart = 0;
      }
    },

    summary: function() {
      var totalMs = Date.now() - _startedAt;
      var idleRatio = _idleSamples > 0 ? _idleTotal / _idleSamples : 0;
      return {
        tab_aways: _tabAways,
        total_hidden_ms: _totalHiddenMs,
        focus_lost_count: _focusLostCount,
        idle_ratio: Math.round(idleRatio * 1000) / 1000,
        activity_count: _activityCount,
        total_ms: totalMs
      };
    }
  };
})();

// ============================================
// Human Verification Challenge
// ============================================

var challengeVerified = false;
var challengeIssuedAt = 0;

function requestChallenge() {
  socket.emit("request_challenge", { type: "thermal" });
}

socket.on("challenge_issued", function(data) {
  challengeIssuedAt = Date.now();
  showChallengeLightbox(data);
});

socket.on("challenge_result", function(data) {
  var lightbox = document.getElementById("challengeLightbox");
  var content = lightbox ? lightbox.querySelector(".challenge-lightbox__content") : null;
  var body = document.getElementById("challengeBody");
  if (!lightbox || !body) return;

  if (data.valid) {
    challengeVerified = true;
    AttentionTracker.stop();

    if (content) content.setAttribute("data-state", "verified");
    body.innerHTML = "";

    var msg = document.createElement("p");
    msg.className = "challenge-result";
    msg.textContent = "calibrated.";
    body.appendChild(msg);
    playSound("affirmative");

    if (data.fingerprint) {
      var fp = document.createElement("span");
      fp.className = "challenge-fingerprint";
      fp.textContent = data.fingerprint;
      body.appendChild(fp);
    }

    var hint = document.getElementById("challengeHint");
    if (hint) hint.textContent = "";

    setTimeout(function() {
      lightbox.style.opacity = "0";
      setTimeout(function() {
        lightbox.style.display = "none";
        lightbox.style.opacity = "";
        if (content) content.removeAttribute("data-state");
      }, 400);
    }, 2000);
  } else {
    var btns = body.querySelectorAll(".challenge-option");
    for (var i = 0; i < btns.length; i++) {
      btns[i].disabled = false;
    }
    var inputEl = body.querySelector(".challenge-input");
    if (inputEl) inputEl.disabled = false;

    var err = body.querySelector(".challenge-error");
    if (!err) {
      err = document.createElement("p");
      err.className = "challenge-error";
      body.appendChild(err);
    }
    err.textContent = data.reason || "try again";
    playSound("negatory");

    // restart attention tracking for retry
    AttentionTracker.stop();
    AttentionTracker.start();
  }
});

function showChallengeLightbox(data) {
  var lightbox = document.getElementById("challengeLightbox");
  var body = document.getElementById("challengeBody");
  var hint = document.getElementById("challengeHint");
  if (!lightbox || !body) return;

  body.innerHTML = "";
  if (hint) hint.textContent = "";

  if (data.type === "thermal") {
    var parsed = JSON.parse(data.prompt);
    var temps = parsed.temps;

    var group = document.createElement("div");
    group.className = "challenge-button-group";

    for (var i = 0; i < temps.length; i++) {
      (function(idx, temp) {
        var btn = document.createElement("button");
        btn.className = "btn challenge-option challenge-option--thermal";
        btn.textContent = temp + "\u00b0";
        var hue = Math.max(0, Math.min(240, 240 - (temp / 100) * 240));
        btn.style.setProperty("--thermal-hue", hue);
        btn.addEventListener("click", function(e) {
          e.stopPropagation();
          var allBtns = group.querySelectorAll(".challenge-option");
          for (var j = 0; j < allBtns.length; j++) allBtns[j].disabled = true;
          var elapsed = Date.now() - challengeIssuedAt;
          var attention = AttentionTracker.summary();
          AttentionTracker.stop();
          socket.emit("verify_challenge", {
            response: idx,
            response_time_ms: elapsed,
            attention: attention
          });
        });
        group.appendChild(btn);
      })(i, temps[i]);
    }

    body.appendChild(group);
    if (hint) hint.textContent = "tap the warmer temperature";

  } else {
    // arithmetic
    var prompt = document.createElement("p");
    prompt.className = "challenge-prompt";
    prompt.textContent = data.prompt + " = ?";
    body.appendChild(prompt);

    var input = document.createElement("input");
    input.type = "number";
    input.className = "challenge-input";
    input.addEventListener("keydown", function(e) {
      if (e.key === "Enter") {
        var elapsed = Date.now() - challengeIssuedAt;
        var attention = AttentionTracker.summary();
        AttentionTracker.stop();
        socket.emit("verify_challenge", {
          response: parseInt(input.value, 10),
          response_time_ms: elapsed,
          attention: attention
        });
        input.disabled = true;
      }
    });
    body.appendChild(input);
    if (hint) hint.textContent = "type the answer and press enter";
  }

  // show lightbox and start attention tracking
  lightbox.style.display = "";
  AttentionTracker.start();
}

// ============================================
// Initialize
// ============================================

setState('idle');
initAudio();

// Request human verification challenge on load
requestChallenge();

// Query-string hydration: ?hydrate=1 triggers token re-hydration
// Used by buff-launch to push tokens into an already-open session
(function() {
  var params = new URLSearchParams(window.location.search);
  if (params.has("hydrate")) {
    // Socket may already be connected (io() at top of file), so check state
    if (socket.connected) {
      socket.emit("reset_session");
      socket.emit("request_hydration");
    } else {
      socket.on("connect", function onHydrate() {
        socket.off("connect", onHydrate);
        socket.emit("reset_session");
        socket.emit("request_hydration");
      });
    }
    // Clean the URL so refreshes don't re-trigger
    params.delete("hydrate");
    var clean = params.toString();
    var newUrl = window.location.pathname + (clean ? "?" + clean : "");
    window.history.replaceState({}, "", newUrl);
  }
})();

// Query-string auto-prompt: ?prompt_file=1 fetches prompt from server, sends as message
// Used by cue-sheet run to kick off the session -- agent speaks first
(function() {
  var params = new URLSearchParams(window.location.search);
  if (params.has("prompt_file")) {
    var doFetch = function() {
      // Delay lets hydration settle when both params are present
      setTimeout(function() {
        socket.emit("request_prompt_file");
      }, 800);
    };
    // Listen for the prompt content coming back
    socket.on("prompt_file_ready", function(data) {
      if (data.text) {
        socket.emit("text_message", { text: data.text });
      }
    });
    if (socket.connected) {
      doFetch();
    } else {
      socket.on("connect", function onAutoPrompt() {
        socket.off("connect", onAutoPrompt);
        doFetch();
      });
    }
    // Clean the URL so refreshes don't re-trigger
    params.delete("prompt_file");
    var clean = params.toString();
    var newUrl = window.location.pathname + (clean ? "?" + clean : "");
    window.history.replaceState({}, "", newUrl);
  }
})();

// Query-string auto-launch: ?cuesheet=quick-task launches a sheet by filename
(function() {
  var params = new URLSearchParams(window.location.search);
  var sheetName = params.get("cuesheet");
  if (!sheetName) return;

  var doLaunch = function() {
    // First get the list to find the full path
    socket.once("cuesheet_list_result", function(data) {
      var sheets = data.sheets || [];
      var match = null;
      for (var i = 0; i < sheets.length; i++) {
        var stem = sheets[i].filename.replace(".yaml", "");
        if (stem === sheetName || sheets[i].name.toLowerCase() === sheetName.toLowerCase()) {
          match = sheets[i];
          break;
        }
      }
      if (match) {
        socket.emit("cuesheet_launch", { path: match.path });
      }
    });
    socket.emit("cuesheet_list");
  };

  if (socket.connected) {
    doLaunch();
  } else {
    socket.on("connect", function onAutoSheet() {
      socket.off("connect", onAutoSheet);
      // Delay to let hydration settle
      setTimeout(doLaunch, 500);
    });
  }

  params.delete("cuesheet");
  var clean = params.toString();
  var newUrl = window.location.pathname + (clean ? "?" + clean : "");
  window.history.replaceState({}, "", newUrl);
})();

// ============================================
// Cue-Sheet Launcher
// ============================================

(function() {
  var toggleBtn = document.getElementById("cuesheetToggle");
  var panel = document.getElementById("cuesheetPanel");
  var closeBtn = document.getElementById("cuesheetPanelClose");
  var listEl = document.getElementById("cuesheetList");

  if (!toggleBtn || !panel) return;

  toggleBtn.addEventListener("click", function() {
    if (panel.style.display === "none") {
      panel.style.display = "block";
      socket.emit("cuesheet_list");
    } else {
      panel.style.display = "none";
    }
  });

  closeBtn.addEventListener("click", function() {
    panel.style.display = "none";
  });

  // Close panel when clicking outside
  document.addEventListener("click", function(e) {
    if (panel.style.display !== "none" &&
        !panel.contains(e.target) &&
        !toggleBtn.contains(e.target)) {
      panel.style.display = "none";
    }
  });

  socket.on("cuesheet_list_result", function(data) {
    listEl.innerHTML = "";
    var sheets = data.sheets || [];
    if (sheets.length === 0) {
      listEl.innerHTML = "<li class=\"list-card__item\"><span class=\"list-card__item-content\">nothing here yet</span></li>";
      return;
    }
    sheets.sort(function(a, b) { return (a.order || 999) - (b.order || 999); });
    for (var i = 0; i < sheets.length; i++) {
      (function(sheet) {
        var item = document.createElement("li");
        item.className = "list-card__item";
        item.style.cursor = "pointer";

        var content = document.createElement("span");
        content.className = "list-card__item-content";

        var name = document.createElement("strong");
        name.className = "list-card__item-title";
        name.style.display = "block";
        name.textContent = (sheet.icon ? sheet.icon + "  " : "") + sheet.name;
        content.appendChild(name);

        var meta = document.createElement("span");
        meta.className = "list-card__item-subtitle";
        meta.style.display = "block";
        meta.style.opacity = "0.7";
        meta.style.fontSize = "0.85em";
        meta.textContent = sheet.description || "";
        content.appendChild(meta);

        item.appendChild(content);

        item.addEventListener("click", function() {
          panel.style.display = "none";
          // Open panel window NOW (on user click) so browser allows it.
          // Socket.IO callback is async and browsers block popups there.
          if (sheet.panel && sheet.panel.url) {
            var pw = sheet.panel.width || 600;
            var ph = sheet.panel.height || 400;
            var pt = sheet.panel.title || sheet.name;
            var pf = "width=" + pw + ",height=" + ph + ",menubar=no,toolbar=no,location=no,status=no,resizable=yes,scrollbars=yes";
            window.open(sheet.panel.url, pt, pf);
          }
          socket.emit("cuesheet_launch", { path: sheet.path });
        });

        listEl.appendChild(item);
      })(sheets[i]);
    }
  });

  socket.on("cuesheet_launched", function(data) {
    // Panel window is opened on click (before this callback) to avoid popup blocker.
    // Send launch message as text to kick off the conversation
    var msg = "Cue-sheet launched: " + data.name + ". " + data.description +
      " (" + data.tokens_created + " tokens created, tag: buff:" + data.slug + ")";
    addSystemMessage(msg);

    // No auto-message to Claude. User speaks when ready.
  });
})();

// ============================================
// Panel PostMessage Bridge
// ============================================
// Any panel window opened via cue-sheet can postMessage back here.
// Origin-checked. Routes events to Claude as text messages.

(function() {
  var ALLOWED_ORIGINS = ["http://localhost:3001"];

  // Map cue-sheet slugs to file paths for panel-initiated launches
  var _cuesheetPathCache = {};

  function _cacheCuesheetPaths() {
    socket.emit("cuesheet_list");
  }

  // Listen for list results to build path cache
  socket.on("cuesheet_list_result", function(data) {
    var sheets = data.sheets || [];
    for (var i = 0; i < sheets.length; i++) {
      var s = sheets[i];
      var slug = s.filename ? s.filename.replace(".yaml", "") : "";
      if (slug) _cuesheetPathCache[slug] = s.path;
    }
  });

  // Also cache child sheets -- request full list including children
  socket.on("cuesheet_children_result", function(data) {
    var sheets = data.sheets || [];
    for (var i = 0; i < sheets.length; i++) {
      var s = sheets[i];
      var slug = s.filename ? s.filename.replace(".yaml", "") : "";
      if (slug) _cuesheetPathCache[slug] = s.path;
    }
  });

  window.addEventListener("message", function(e) {
    if (ALLOWED_ORIGINS.indexOf(e.origin) === -1) return;
    if (!e.data || !e.data.type) return;

    if (e.data.type === "rom_loaded" && e.data.title) {
      socket.emit("speak", {text: e.data.title + " loaded."});
    }

    if (e.data.type === "rom_ejected" && e.data.title) {
      socket.emit("speak", {text: e.data.title + " ejected."});
    }

    if (e.data.type === "game_over" && e.data.slug) {
      socket.emit("arcade_game_over", {slug: e.data.slug, title: e.data.title || e.data.slug});
    }

    // Panel-initiated cue-sheet launch
    if (e.data.type === "launch_cuesheet" && e.data.slug) {
      var slug = e.data.slug;
      var path = _cuesheetPathCache[slug];
      if (path) {
        socket.emit("cuesheet_launch", {path: path});
      } else {
        // Try constructing path directly
        socket.emit("cuesheet_launch", {path: "cue-sheets/" + slug + ".yaml"});
      }
    }

    // Panel-initiated TTS
    if (e.data.type === "panel_speak" && e.data.text) {
      socket.emit("speak", {text: e.data.text});
    }

    // Quick action with pre-filled data
    if (e.data.type === "quick_action" && e.data.task) {
      addSystemMessage("Quick action: " + e.data.task + " (urgency: " + e.data.urgency + ")");
    }
  });

  // Pre-cache paths on load
  _cacheCuesheetPaths();
})();

// ============================================
// Cue-Sheet Sign-Off Gate
// ============================================

socket.on("cuesheet_signoff_request", function(data) {
  var card = document.createElement("article");
  card.className = "card assistant";

  var header = document.createElement("header");
  header.className = "card__header";
  var icon = document.createElement("span");
  icon.className = "card__icon";
  icon.setAttribute("aria-hidden", "true");
  var titleEl = document.createElement("h3");
  titleEl.className = "card__title";
  titleEl.textContent = "Sign-Off";
  header.appendChild(icon);
  header.appendChild(titleEl);

  var body = document.createElement("div");
  body.className = "card__body";

  var questionEl = document.createElement("p");
  questionEl.className = "card__description";
  questionEl.textContent = data.question || ("Sign off on '" + data.title + "'?");
  body.appendChild(questionEl);

  var buttonGroup = document.createElement("div");
  buttonGroup.className = "button-group";

  var yesBtn = document.createElement("button");
  yesBtn.className = "btn btn--primary";
  yesBtn.textContent = "Yes";

  var noBtn = document.createElement("button");
  noBtn.className = "btn btn--secondary";
  noBtn.textContent = "No";

  function handleSignoff(answer) {
    playSound(answer === "Yes" ? "affirmative" : "negatory");
    socket.emit("cuesheet_signoff_response", {
      answer: answer,
      source_hash: data.source_hash || "",
      cuesheet_name: data.title || "",
      doc_id: data.doc_id || "",
      result_token_id: data.result_token_id || ""
    });
    yesBtn.disabled = true;
    noBtn.disabled = true;
    yesBtn.classList.add("disabled");
    noBtn.classList.add("disabled");

    var indicator = document.createElement("div");
    indicator.className = "choice-indicator";
    indicator.textContent = "Selected: " + answer;
    buttonGroup.parentNode.appendChild(indicator);

    addMessage("user", answer);
  }

  yesBtn.addEventListener("click", function(e) {
    e.stopPropagation();
    handleSignoff("Yes");
  });

  noBtn.addEventListener("click", function(e) {
    e.stopPropagation();
    handleSignoff("No");
  });

  buttonGroup.appendChild(yesBtn);
  buttonGroup.appendChild(noBtn);
  body.appendChild(buttonGroup);

  card.appendChild(header);
  card.appendChild(body);

  conversation.appendChild(card);
  conversation.scrollTop = conversation.scrollHeight;
});

// ============================================
// Gallery Strip + Lightbox
// ============================================

var galleryRegistry = {};
var galleryIdCounter = 0;
var galleryLightboxOpen = false;
var galleryLightboxState = { galleryId: null, index: 0 };
var galleryNarrating = false;
var pinnedGalleries = {};
var galleryTokenMap = {};   // galleryId -> tokenId
var tokenGalleryMap = {};   // tokenId -> galleryId

function safeEncodeComponent(str) {
  try {
    return encodeURIComponent(decodeURIComponent(str));
  } catch (e) {
    return encodeURIComponent(str);
  }
}

var _galleryVideoExts = [".mp4", ".mov", ".webm", ".m4v"];

function isGalleryVideo(item) {
  if (item && item.type === "video") return true;
  var fname = (item && (item.filename || item.src)) || "";
  var dot = fname.lastIndexOf(".");
  if (dot < 0) return false;
  return _galleryVideoExts.indexOf(fname.substring(dot).toLowerCase()) >= 0;
}

function resolveGalleryImageUrl(img) {
  if (!img) {
    console.warn("[gallery] resolveGalleryImageUrl called with null/undefined image");
    return "";
  }
  // Format 1: structured slug + filename (port-aware when available)
  if (img.slug && img.filename) {
    // Dropped images served from /drops/ route
    if (img.slug === "_drops" || img.slug === "_hot_loose" || img.slug === "_cold_loose") {
      var url = "/drops/" + safeEncodeComponent(img.filename);
      console.log("[gallery] resolved drops: " + url);
      return url;
    }
    var port = (img.port || "cold").toLowerCase();
    var url = "/vault/" + port + "/" + safeEncodeComponent(img.slug) + "/" + safeEncodeComponent(img.filename);
    console.log("[gallery] resolved slug+filename (" + port + "): " + url);
    return url;
  }
  // Format 2: src path -- convert known patterns to port-aware URLs
  if (img.src) {
    var src = img.src;
    if (src.charAt(0) === "/") src = src.substring(1);
    // Strip vault/ prefix if present (e.g. vault/HOT/slug/file.png)
    var srcLower = src.toLowerCase();
    if (srcLower.indexOf("vault/") === 0) {
      src = src.substring(6);
      srcLower = src.toLowerCase();
    }
    // hot/<slug>/<filename> or HOT/<slug>/<filename>
    if (srcLower.indexOf("hot/") === 0) {
      var rest = src.substring(4);
      var idx = rest.indexOf("/");
      if (idx > 0) {
        var slug = rest.substring(0, idx);
        var filename = rest.substring(idx + 1);
        if (filename.indexOf("images/") === 0) filename = filename.substring(7);
        var url = "/vault/hot/" + safeEncodeComponent(slug) + "/" + safeEncodeComponent(filename);
        console.log("[gallery] resolved hot: " + img.src + " -> " + url);
        return url;
      }
    }
    // cold/<slug>/<filename> or COLD/<slug>/<filename>
    if (srcLower.indexOf("cold/") === 0) {
      var rest = src.substring(5);
      var idx = rest.indexOf("/");
      if (idx > 0) {
        var slug = rest.substring(0, idx);
        var filename = rest.substring(idx + 1);
        if (filename.indexOf("images/") === 0) filename = filename.substring(7);
        var url = "/vault/cold/" + safeEncodeComponent(slug) + "/" + safeEncodeComponent(filename);
        console.log("[gallery] resolved cold: " + img.src + " -> " + url);
        return url;
      }
    }
    // ACTIVE/<slug>/<filename> -- treat as hot
    if (src.indexOf("ACTIVE/") === 0) {
      var rest = src.substring(7);
      var idx = rest.indexOf("/");
      if (idx > 0) {
        var slug = rest.substring(0, idx);
        var filename = rest.substring(idx + 1);
        if (filename.indexOf("images/") === 0) filename = filename.substring(7);
        var url = "/vault/hot/" + safeEncodeComponent(slug) + "/" + safeEncodeComponent(filename);
        console.log("[gallery] resolved ACTIVE as hot: " + img.src + " -> " + url);
        return url;
      }
    }
    // docs/vault/<slug>/images/<filename> -- treat as cold
    if (src.indexOf("docs/vault/") === 0) {
      var rest = src.substring(11);
      var parts = rest.split("/");
      if (parts.length >= 3 && parts[1] === "images") {
        var url = "/vault/cold/" + safeEncodeComponent(parts[0]) + "/" + safeEncodeComponent(parts.slice(2).join("/"));
        console.log("[gallery] resolved docs/vault as cold: " + img.src + " -> " + url);
        return url;
      }
    }
    console.warn("[gallery] unrecognized src pattern, passing through: " + img.src);
    return img.src;
  }
  console.warn("[gallery] image object has no slug/filename and no src: " + JSON.stringify(img));
  return "";
}

function createGalleryStrip(data) {
  var id = "gallery_" + (++galleryIdCounter);
  var images = data.images || [];

  // Validate and filter images
  var valid = [];
  for (var v = 0; v < images.length; v++) {
    var item = images[v];
    if (!item) {
      console.warn("[gallery] skipping null image at index " + v);
      continue;
    }
    if (!item.slug && !item.filename && !item.src) {
      console.warn("[gallery] skipping image at index " + v + " -- no slug, filename, or src: " + JSON.stringify(item));
      continue;
    }
    valid.push(item);
  }
  images = valid;

  if (images.length === 0) {
    console.warn("[gallery] no valid images after filtering, returning null");
    return null;
  }

  galleryRegistry[id] = { images: images, title: data.title || "" };

  var figure = document.createElement("figure");
  var layout = images.length === 1 ? "gallery-hero"
             : images.length === 2 ? "gallery-compare"
             : "gallery-strip";
  figure.className = layout;
  figure.setAttribute("data-gallery-id", id);

  // Header bar: caption (left) + icons (right)
  var header = document.createElement("div");
  header.className = "gallery__header";

  var caption = document.createElement("figcaption");
  caption.className = "gallery-strip__title";
  caption.textContent = data.title || "";
  header.appendChild(caption);

  var actions = document.createElement("div");
  actions.className = "gallery__actions";

  // Case study icon
  var caseBtn = document.createElement("button");
  caseBtn.className = "case-study-icon";
  caseBtn.setAttribute("aria-label", "Open as case study");
  caseBtn.setAttribute("title", "open as case study");
  caseBtn.setAttribute("data-gallery-id", id);

  var caseGlyph = document.createElement("span");
  caseGlyph.className = "case-study-icon__glyph";
  caseBtn.appendChild(caseGlyph);

  caseBtn.addEventListener("click", function(e) {
    e.stopPropagation();
    openCaseStudy(id, figure);
  });
  actions.appendChild(caseBtn);

  // Pin icon
  var pinBtn = document.createElement("button");
  pinBtn.className = "pin-icon";
  pinBtn.setAttribute("data-state", "inactive");
  pinBtn.setAttribute("data-gallery-id", id);
  pinBtn.setAttribute("aria-label", "Pin gallery");
  pinBtn.setAttribute("title", "pin gallery");

  var pinGlyph = document.createElement("span");
  pinGlyph.className = "pin-icon__glyph";
  pinBtn.appendChild(pinGlyph);

  pinBtn.addEventListener("click", function(e) {
    e.stopPropagation();
    var state = pinBtn.getAttribute("data-state");
    if (state === "inactive") {
      pinGallery(id);
      pinBtn.setAttribute("data-state", "active");
    } else {
      unpinGallery(id);
      pinBtn.setAttribute("data-state", "inactive");
    }
  });
  actions.appendChild(pinBtn);

  header.appendChild(actions);
  figure.appendChild(header);

  var track = document.createElement("div");
  track.className = "gallery-strip__track";
  track.setAttribute("role", "list");

  for (var i = 0; i < images.length; i++) {
    (function(idx) {
      var img = images[idx];
      var btn = document.createElement("button");
      btn.className = "gallery-strip__item";
      btn.setAttribute("data-index", idx);
      btn.setAttribute("role", "listitem");

      var isVideo = isGalleryVideo(img);
      var thumb;
      if (isVideo) {
        thumb = document.createElement("video");
        thumb.className = "gallery-strip__thumb";
        thumb.src = resolveGalleryImageUrl(img);
        thumb.preload = "metadata";
        thumb.muted = true;
        thumb.playsInline = true;
        thumb.onerror = function() {
          console.error("[gallery] video thumbnail failed to load: " + this.src);
          btn.classList.add("gallery-strip__item--error");
        };
        btn.appendChild(thumb);
        var badge = document.createElement("span");
        badge.className = "gallery-strip__play-badge";
        badge.setAttribute("aria-hidden", "true");
        btn.appendChild(badge);
      } else {
        thumb = document.createElement("img");
        thumb.className = "gallery-strip__thumb";
        thumb.src = resolveGalleryImageUrl(img);
        thumb.alt = img.caption || img.filename || "";
        thumb.loading = "lazy";
        thumb.onerror = function() {
          console.error("[gallery] thumbnail failed to load: " + this.src);
          this.src = "";
          this.alt = "Image not found";
          btn.classList.add("gallery-strip__item--error");
        };
        btn.appendChild(thumb);
      }

      if (img.caption) {
        var cap = document.createElement("span");
        cap.className = "gallery-strip__caption";
        cap.textContent = img.caption;
        btn.appendChild(cap);
      }

      btn.addEventListener("click", function() {
        openGalleryLightbox(id, idx);
      });

      track.appendChild(btn);
    })(i);
  }

  figure.appendChild(track);

  var count = document.createElement("p");
  count.className = "gallery-strip__count";
  var hasVideo = images.some(function(m) { return isGalleryVideo(m); });
  var hasImage = images.some(function(m) { return !isGalleryVideo(m); });
  var unit = (hasVideo && hasImage) ? "item" : hasVideo ? "video" : "image";
  count.textContent = images.length + " " + unit + (images.length !== 1 ? "s" : "");
  figure.appendChild(count);

  return figure;
}

function openCaseStudy(galleryId, galleryFigure) {
  // Walk up to find the parent message card
  var card = galleryFigure.closest("article.card");
  var rawText = card ? (card.dataset.rawText || "") : "";
  var entry = galleryRegistry[galleryId];
  if (!entry) return;

  // Build payload
  var payload = JSON.stringify({
    text: rawText,
    gallery: { images: entry.images, title: entry.title },
    galleryId: galleryId
  });

  // Submit via hidden form (POST to new tab)
  var form = document.createElement("form");
  form.method = "POST";
  form.action = "/case-study";
  form.target = "_blank";
  form.style.display = "none";

  var input = document.createElement("input");
  input.type = "hidden";
  input.name = "json";
  input.value = payload;
  form.appendChild(input);

  document.body.appendChild(form);
  form.submit();
  document.body.removeChild(form);
}

function openGalleryLightbox(galleryId, startIndex) {
  var entry = galleryRegistry[galleryId];
  if (!entry) return;
  var images = entry.images;
  if (!images || images.length === 0) return;

  galleryLightboxState.galleryId = galleryId;
  galleryLightboxState.index = startIndex || 0;
  galleryLightboxOpen = true;

  var lb = document.getElementById("galleryLightbox");
  lb.style.display = "";

  // Show Let Go only for pinned galleries
  var letGoBtn = document.getElementById("galleryLetGo");
  if (letGoBtn) {
    letGoBtn.style.display = galleryTokenMap[galleryId] ? "" : "none";
  }

  renderGallerySlide();
}

// Zoom state for lightbox image
var galleryZoom = 1.0;
var GALLERY_ZOOM_STEP = 0.25;
var GALLERY_ZOOM_MIN = 0.5;
var GALLERY_ZOOM_MAX = 4.0;

function applyGalleryZoom() {
  var el = document.getElementById("galleryLightboxImage");
  if (!el || el.tagName === "VIDEO") return;
  el.style.transform = galleryZoom === 1.0 ? "" : "scale(" + galleryZoom + ")";
}

// Send an image to the vision model for captioning
function _captionImage(imgSrc, context, callback) {
  fetch(imgSrc).then(function(r) { return r.blob(); }).then(function(blob) {
    var reader = new FileReader();
    reader.onload = function() {
      var prompt;
      if (context) {
        prompt = "The viewer says: \"" + context + "\". Distill that into a tight caption for this image. Under 10 words. Keep what matters, drop the rest. No quotes, no emoji.";
      }
      fetch("/api/describe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ images: [reader.result], prompt: prompt || undefined })
      }).then(function(r) { return r.ok ? r.json() : null; })
        .then(function(data) {
          callback(data && data.result ? data.result : null);
        }).catch(function() { callback(null); });
    };
    reader.readAsDataURL(blob);
  }).catch(function() { callback(null); });
}

// Update caption in registry and any visible thumbnail caption
function _persistCaption(galleryId, idx, text) {
  var entry = galleryRegistry[galleryId];
  if (entry && entry.images[idx]) entry.images[idx].caption = text;
  // Update thumbnail caption if visible
  var strip = document.querySelector("[data-gallery-id='" + galleryId + "']");
  if (strip) {
    var item = strip.querySelector("[data-index='" + idx + "']");
    if (item) {
      var cap = item.querySelector(".gallery-strip__caption");
      if (!cap) {
        cap = document.createElement("span");
        cap.className = "gallery-strip__caption";
        item.appendChild(cap);
      }
      cap.textContent = text;
    }
  }
}

function renderGallerySlide() {
  var entry = galleryRegistry[galleryLightboxState.galleryId];
  if (!entry) return;
  var images = entry.images;

  var idx = galleryLightboxState.index;
  var img = images[idx];

  // Reset zoom on every slide change
  galleryZoom = 1.0;

  // Update metadata
  var title = document.getElementById("galleryLightboxTitle");
  var strip = document.querySelector(
    "[data-gallery-id='" + galleryLightboxState.galleryId + "']"
  );
  var figcaption = strip ? strip.querySelector(".gallery-strip__title") : null;
  title.textContent = figcaption ? figcaption.textContent : "";

  var counter = document.getElementById("galleryLightboxCounter");
  counter.textContent = (idx + 1) + " / " + images.length;

  var captionEl = document.getElementById("galleryLightboxCaption");
  var refineLink = document.getElementById("galleryRefine");
  var refineInputWrap = document.getElementById("galleryRefineInput");
  var refineText = document.getElementById("galleryRefineText");

  // Hide refine input on slide change
  if (refineInputWrap) refineInputWrap.style.display = "none";
  if (refineText) refineText.value = "";

  if (img.caption) {
    captionEl.textContent = img.caption;
    captionEl.classList.remove("gallery-lightbox__caption--generating");
    if (refineLink) refineLink.style.display = "";
  } else {
    // Auto-caption: fire vision model
    captionEl.textContent = "captioning...";
    captionEl.classList.add("gallery-lightbox__caption--generating");
    if (refineLink) refineLink.style.display = "none";

    var mediaSrc = resolveGalleryImageUrl(img);
    var captureGalleryId = galleryLightboxState.galleryId;
    var captureIdx = idx;
    _captionImage(mediaSrc, null, function(result) {
      // Only update if still viewing the same slide
      if (galleryLightboxState.galleryId === captureGalleryId &&
          galleryLightboxState.index === captureIdx) {
        if (result) {
          captionEl.textContent = result;
          captionEl.classList.remove("gallery-lightbox__caption--generating");
          if (refineLink) refineLink.style.display = "";
        } else {
          captionEl.textContent = "";
          captionEl.classList.remove("gallery-lightbox__caption--generating");
          if (refineLink) {
            refineLink.style.display = "";
            refineLink.textContent = "describe";
          }
        }
      }
      if (result) _persistCaption(captureGalleryId, captureIdx, result);
    });
  }

  document.getElementById("galleryPrev").style.display = images.length > 1 ? "" : "none";
  document.getElementById("galleryNext").style.display = images.length > 1 ? "" : "none";

  // Replace the media element entirely -- prevents stale pixels / lingering playback
  var oldEl = document.getElementById("galleryLightboxImage");
  var stage = oldEl.parentNode;
  var newEl;
  var mediaSrc = resolveGalleryImageUrl(img);

  if (isGalleryVideo(img)) {
    newEl = document.createElement("video");
    newEl.className = "gallery-lightbox__image gallery-lightbox__video";
    newEl.id = "galleryLightboxImage";
    newEl.controls = true;
    newEl.autoplay = true;
    newEl.playsInline = true;
    newEl.onloadeddata = function() {
      newEl.classList.add("gallery-lightbox__image--visible");
    };
    newEl.onerror = function() {
      console.error("[gallery] lightbox video failed to load: " + this.src);
      newEl.classList.add("gallery-lightbox__image--visible");
      newEl.classList.add("gallery-lightbox__image--error");
    };
    newEl.src = mediaSrc;
  } else {
    newEl = document.createElement("img");
    newEl.className = "gallery-lightbox__image";
    newEl.id = "galleryLightboxImage";
    newEl.alt = img.caption || img.filename || "";
    newEl.onload = function() {
      newEl.classList.add("gallery-lightbox__image--visible");
    };
    newEl.onerror = function() {
      console.error("[gallery] lightbox image failed to load: " + this.src);
      newEl.classList.add("gallery-lightbox__image--visible");
      newEl.classList.add("gallery-lightbox__image--error");
    };
    newEl.src = mediaSrc;
  }
  stage.replaceChild(newEl, oldEl);
}

function navigateGallery(direction) {
  var entry = galleryRegistry[galleryLightboxState.galleryId];
  if (!entry) return;
  var images = entry.images;

  var next = galleryLightboxState.index + direction;
  if (next < 0) next = images.length - 1;
  if (next >= images.length) next = 0;
  galleryLightboxState.index = next;

  renderGallerySlide();
}

function closeGalleryLightbox() {
  if (galleryNarrating) {
    socket.emit("interrupt");
    galleryNarrating = false;
    var narBtn = document.getElementById("galleryNarrate");
    if (narBtn) narBtn.innerHTML = "&#9654;";
  }
  galleryLightboxOpen = false;
  galleryLightboxState.galleryId = null;
  galleryLightboxState.index = 0;

  // Pause video if playing, then replace element to clear stale pixels
  galleryZoom = 1.0;
  var oldEl = document.getElementById("galleryLightboxImage");
  if (oldEl && oldEl.tagName === "VIDEO") {
    oldEl.pause();
    oldEl.removeAttribute("src");
    oldEl.load();
  }
  var stage = oldEl.parentNode;
  var freshEl = document.createElement("img");
  freshEl.className = "gallery-lightbox__image";
  freshEl.id = "galleryLightboxImage";
  freshEl.alt = "";
  stage.replaceChild(freshEl, oldEl);

  var lb = document.getElementById("galleryLightbox");
  lb.style.display = "none";
}

function pinGallery(galleryId) {
  // Avoid duplicates
  if (pinnedGalleries[galleryId]) return;

  var entry = galleryRegistry[galleryId];
  if (!entry) return;

  // Emit to backend -- token_created handler builds the thumbnail
  socket.emit("pin_gallery", {
    gallery_id: galleryId,
    title: entry.title || "Gallery",
    images: entry.images || []
  });
}

function unpinGallery(galleryId) {
  var tokenId = galleryTokenMap[galleryId];
  if (tokenId) {
    // Remove backend modifier + thumbnail via existing flow
    socket.emit("remove_modifier", { token_id: tokenId });
    removeModifierThumbnail(tokenId);
    delete tokenGalleryMap[tokenId];
  }
  // Clean up local-only thumb if it somehow exists (legacy)
  var thumb = pinnedGalleries[galleryId];
  if (thumb && thumb.parentNode) {
    thumb.parentNode.removeChild(thumb);
  }
  delete pinnedGalleries[galleryId];
  delete galleryTokenMap[galleryId];

  // Reset pin icon on the gallery strip
  var strip = document.querySelector("[data-gallery-id='" + galleryId + "'].gallery-strip");
  if (strip) {
    var pinIcon = strip.querySelector(".pin-icon");
    if (pinIcon) pinIcon.setAttribute("data-state", "inactive");
  }

  updateClearAllVisibility();
}

// Gallery lightbox event wiring
(function() {
  var closeBtn = document.getElementById("galleryLightboxClose");
  var prevBtn = document.getElementById("galleryPrev");
  var nextBtn = document.getElementById("galleryNext");
  var backdrop = document.querySelector(".gallery-lightbox__backdrop");
  var narrateBtn = document.getElementById("galleryNarrate");
  var letGoBtn = document.getElementById("galleryLetGo");

  if (closeBtn) closeBtn.addEventListener("click", closeGalleryLightbox);
  if (prevBtn) prevBtn.addEventListener("click", function() { navigateGallery(-1); });
  if (nextBtn) nextBtn.addEventListener("click", function() { navigateGallery(1); });
  if (backdrop) backdrop.addEventListener("click", closeGalleryLightbox);

  // Narrate button: play/stop caption TTS
  if (narrateBtn) {
    narrateBtn.addEventListener("click", function() {
      if (galleryNarrating) {
        socket.emit("interrupt");
        galleryNarrating = false;
        narrateBtn.innerHTML = "&#9654;";
      } else {
        var caption = document.getElementById("galleryLightboxCaption");
        var text = caption ? caption.textContent : "";
        if (!text) return;
        socket.emit("narrate_caption", { text: text });
        galleryNarrating = true;
        narrateBtn.innerHTML = "&#9724;";
      }
    });
  }

  // Refine link: voice-driven caption refinement via follow-up system
  var refineLink = document.getElementById("galleryRefine");

  if (refineLink) {
    refineLink.addEventListener("click", function(e) {
      e.preventDefault();
      var entry = galleryRegistry[galleryLightboxState.galleryId];
      if (!entry) return;
      var idx = galleryLightboxState.index;
      var img = entry.images[idx];
      var captureGalleryId = galleryLightboxState.galleryId;
      var captureSrc = resolveGalleryImageUrl(img);
      var currentCaption = img.caption || "";

      // Start follow-up
      startFollowUp({
        type: "caption_refine",
        context: {
          galleryId: captureGalleryId,
          index: idx,
          src: captureSrc,
          currentCaption: currentCaption
        },
        generate: function(userInput, cb) {
          var context = userInput;
          if (currentCaption) {
            context = "Current caption: \"" + currentCaption + "\". User says: " + userInput;
          }
          _captionImage(captureSrc, context, cb);
        },
        onConfirm: function(proposal) {
          _persistCaption(captureGalleryId, idx, proposal);
          // Update strip caption
          var stripEl = document.querySelector("[data-gallery-id='" + captureGalleryId + "']");
          if (stripEl) {
            var item = stripEl.querySelector("[data-index='" + idx + "']");
            if (item) {
              var cap = item.querySelector(".gallery-strip__caption");
              if (!cap) {
                cap = document.createElement("span");
                cap.className = "gallery-strip__caption";
                item.appendChild(cap);
              }
              cap.textContent = proposal;
            }
          }
        }
      });

      // Close lightbox, open drawer
      closeGalleryLightbox();
      requestAnimationFrame(function() {
        drawer.classList.add("open");

        // Show image + prompt
        var card = document.createElement("article");
        card.className = "card assistant";
        card.dataset.timestamp = Date.now();
        var body = document.createElement("div");
        body.className = "card__body";

        var thumb = document.createElement("img");
        thumb.className = "refine-thumb";
        thumb.src = captureSrc;
        thumb.alt = "image to refine";
        body.appendChild(thumb);

        if (currentCaption) {
          var current = document.createElement("p");
          current.className = "drop-status";
          current.textContent = "current: " + currentCaption;
          body.appendChild(current);
        }

        var p = document.createElement("p");
        p.textContent = "tell me what you see -- I'll refine the caption.";
        body.appendChild(p);

        card.appendChild(body);
        conversation.appendChild(card);
        conversation.scrollTop = conversation.scrollHeight;

        socket.emit("speak", { text: "tell me what you see. I'll refine the caption." });
      });
    });
  }

  // Listen for narration_done from server
  socket.on("narration_done", function() {
    galleryNarrating = false;
    var btn = document.getElementById("galleryNarrate");
    if (btn) btn.innerHTML = "&#9654;";
    // Clear paragraph-level TTS highlight
    var active = document.querySelector(".tts-speaking");
    if (active) active.classList.remove("tts-speaking");
  });

  // Let Go button: unpin gallery from lightbox
  if (letGoBtn) {
    letGoBtn.addEventListener("click", function() {
      if (galleryLightboxState.galleryId) {
        unpinGallery(galleryLightboxState.galleryId);
        letGoBtn.style.display = "none";
      }
    });
  }

  document.addEventListener("keydown", function(e) {
    if (!galleryLightboxOpen) return;
    if (e.key === "Escape") {
      e.preventDefault();
      closeGalleryLightbox();
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      navigateGallery(-1);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      navigateGallery(1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      galleryZoom = Math.min(galleryZoom + GALLERY_ZOOM_STEP, GALLERY_ZOOM_MAX);
      applyGalleryZoom();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      galleryZoom = Math.max(galleryZoom - GALLERY_ZOOM_STEP, GALLERY_ZOOM_MIN);
      applyGalleryZoom();
    }
  });
})();

// ============================================
// Cue-Card Activity Stream
// ============================================

var streamCards = {};               // token_id -> DOM element
var trackPiles = {};                // track_name -> { tokenIds, featuredIndex, rotationInterval, el }
var streamDecayIntervals = {};      // token_id -> interval ID
var CUE_PILE_ROTATION_MS = 10000;

function getTrackTag(tags) {
  if (!tags || !tags.length) return null;
  for (var i = 0; i < tags.length; i++) {
    if (tags[i].indexOf("track:") === 0) {
      return tags[i].substring(6);
    }
  }
  return null;
}

function formatStreamAge(createdAt) {
  if (!createdAt) return "";
  var ms = Date.now() - new Date(createdAt).getTime();
  var mins = Math.floor(ms / 60000);
  if (mins < 1) return "now";
  if (mins < 60) return mins + "m";
  var hours = Math.floor(mins / 60);
  if (hours < 24) return hours + "h";
  return Math.floor(hours / 24) + "d";
}

function formatTokenLabel(data) {
  var raw = data.label || data.key || "token";

  // Cue tokens: show the objective text
  if (data.type === "cue" && raw.indexOf("cue_") === 0) {
    if (data.value && data.value.length <= 60) return data.value;
    var stripped = raw.replace(/^cue_/, "").replace(/_[a-z]+-[a-z]+.*$/, "");
    return stripped.charAt(0).toUpperCase() + stripped.slice(1);
  }

  // Session tokens: show the sheet name
  if (raw.indexOf("cuesheet_session_") === 0) {
    var slug = raw.replace("cuesheet_session_", "");
    return slug.split("-").map(function(w) { return w.charAt(0).toUpperCase() + w.slice(1); }).join(" ");
  }

  // Modifier tokens: clean display
  if (raw.indexOf("mod_holdon_") === 0) return "Hold";
  if (raw.indexOf("mod_cuesheet_context_") === 0) return "Context";
  if (raw.indexOf("mod_") === 0) {
    var modName = raw.replace(/^mod_/, "").replace(/_[a-z]+-[a-z]+.*$/, "");
    return modName.split("_").map(function(w) { return w.charAt(0).toUpperCase() + w.slice(1); }).join(" ");
  }

  // Buff tokens
  if (raw.indexOf("buff_cuesheet_") === 0) {
    var bSlug = raw.replace("buff_cuesheet_", "");
    return bSlug.split("-").map(function(w) { return w.charAt(0).toUpperCase() + w.slice(1); }).join(" ");
  }

  // General cleanup: replace underscores with spaces, title case
  if (raw.indexOf("_") !== -1) {
    return raw.split("_").map(function(w) { return w.charAt(0).toUpperCase() + w.slice(1); }).join(" ");
  }

  return raw;
}

function addCueCardToStream(tokenId) {
  // Skip if already in stream
  if (streamCards[tokenId]) return;
  // Need registry data
  var data = tokenRegistry[tokenId];
  if (!data) return;

  var trackName = getTrackTag(data.tags);
  if (trackName) {
    addCueCardToPile(tokenId, trackName);
  } else {
    createIndividualCueCard(tokenId);
  }
  startStreamDecay(tokenId);
}

function createIndividualCueCard(tokenId) {
  var container = document.getElementById("cueStream");
  if (!container) return;

  var data = tokenRegistry[tokenId];
  if (!data) return;

  var card = document.createElement("div");
  card.className = "cue-card";
  card.setAttribute("data-stream-id", tokenId);

  var dot = document.createElement("span");
  dot.className = "cue-card__dot";
  dot.setAttribute("data-type", data.type);
  card.appendChild(dot);

  var label = document.createElement("span");
  label.className = "cue-card__label";
  label.textContent = formatTokenLabel(data);
  card.appendChild(label);

  var age = document.createElement("span");
  age.className = "cue-card__age";
  age.textContent = formatStreamAge(data.created_at);
  card.appendChild(age);

  card.addEventListener("click", function() {
    pinFromStream(tokenId);
  });

  // Insert at top
  if (container.firstChild) {
    container.insertBefore(card, container.firstChild);
  } else {
    container.appendChild(card);
  }

  streamCards[tokenId] = card;
}

function addCueCardToPile(tokenId, trackName) {
  if (!trackPiles[trackName]) {
    createPileElement(trackName);
  }

  var pile = trackPiles[trackName];
  pile.tokenIds.push(tokenId);
  // Store reference to pile element
  streamCards[tokenId] = pile.el;
  updatePileDisplay(trackName);

  if (!pile.rotationInterval && pile.tokenIds.length > 1) {
    startPileRotation(trackName);
  }
}

function createPileElement(trackName) {
  var container = document.getElementById("cueStream");
  if (!container) return;

  var pile = document.createElement("div");
  pile.className = "cue-pile";
  pile.setAttribute("data-track", trackName);

  var header = document.createElement("div");
  header.className = "cue-pile__header";

  var name = document.createElement("span");
  name.className = "cue-pile__name";
  name.textContent = trackName;
  header.appendChild(name);

  var count = document.createElement("span");
  count.className = "cue-pile__count";
  count.textContent = "";
  header.appendChild(count);

  header.addEventListener("click", function() {
    pile.classList.toggle("cue-pile--expanded");
    updatePileDisplay(trackName);
  });

  pile.appendChild(header);

  var viewport = document.createElement("div");
  viewport.className = "cue-pile__viewport";
  pile.appendChild(viewport);

  var members = document.createElement("div");
  members.className = "cue-pile__members";
  pile.appendChild(members);

  // Insert at top of stream
  if (container.firstChild) {
    container.insertBefore(pile, container.firstChild);
  } else {
    container.appendChild(pile);
  }

  trackPiles[trackName] = {
    tokenIds: [],
    featuredIndex: 0,
    rotationInterval: null,
    el: pile
  };
}

function updatePileDisplay(trackName) {
  var pile = trackPiles[trackName];
  if (!pile) return;
  var el = pile.el;

  // Update count badge
  var countEl = el.querySelector(".cue-pile__count");
  if (countEl) {
    countEl.textContent = pile.tokenIds.length > 0 ? pile.tokenIds.length : "";
  }

  if (el.classList.contains("cue-pile--expanded")) {
    renderPileMembers(trackName);
  } else {
    renderFeaturedCard(trackName);
  }
}

function renderFeaturedCard(trackName) {
  var pile = trackPiles[trackName];
  if (!pile || pile.tokenIds.length === 0) return;

  var viewport = pile.el.querySelector(".cue-pile__viewport");
  if (!viewport) return;

  // Clamp index
  if (pile.featuredIndex >= pile.tokenIds.length) {
    pile.featuredIndex = 0;
  }

  var tokenId = pile.tokenIds[pile.featuredIndex];
  var data = tokenRegistry[tokenId];
  if (!data) return;

  viewport.innerHTML = "";

  var card = document.createElement("div");
  card.className = "cue-card";
  card.setAttribute("data-stream-id", tokenId);

  var dot = document.createElement("span");
  dot.className = "cue-card__dot";
  dot.setAttribute("data-type", data.type);
  card.appendChild(dot);

  var label = document.createElement("span");
  label.className = "cue-card__label";
  label.textContent = formatTokenLabel(data);
  card.appendChild(label);

  card.addEventListener("click", function() {
    pinFromStream(tokenId);
  });

  viewport.appendChild(card);
}

function renderPileMembers(trackName) {
  var pile = trackPiles[trackName];
  if (!pile) return;

  var membersEl = pile.el.querySelector(".cue-pile__members");
  if (!membersEl) return;

  membersEl.innerHTML = "";

  for (var i = 0; i < pile.tokenIds.length; i++) {
    var tokenId = pile.tokenIds[i];
    var data = tokenRegistry[tokenId];
    if (!data) continue;

    var card = document.createElement("div");
    card.className = "cue-card";
    card.setAttribute("data-stream-id", tokenId);

    var dot = document.createElement("span");
    dot.className = "cue-card__dot";
    dot.setAttribute("data-type", data.type);
    card.appendChild(dot);

    var label = document.createElement("span");
    label.className = "cue-card__label";
    label.textContent = formatTokenLabel(data);
    card.appendChild(label);

    (function(tid) {
      card.addEventListener("click", function() {
        pinFromStream(tid);
      });
    })(tokenId);

    membersEl.appendChild(card);
  }
}

function startPileRotation(trackName) {
  var pile = trackPiles[trackName];
  if (!pile) return;
  if (pile.rotationInterval) return;

  pile.rotationInterval = setInterval(function() {
    if (pile.el.classList.contains("cue-pile--expanded")) return;
    if (pile.tokenIds.length <= 1) return;

    var viewport = pile.el.querySelector(".cue-pile__viewport");
    var featured = viewport ? viewport.querySelector(".cue-card") : null;
    if (featured) {
      featured.classList.add("cue-pile__featured--exiting");
    }

    setTimeout(function() {
      pile.featuredIndex = (pile.featuredIndex + 1) % pile.tokenIds.length;
      renderFeaturedCard(trackName);
    }, 250);
  }, CUE_PILE_ROTATION_MS);
}

function startStreamDecay(tokenId) {
  var data = tokenRegistry[tokenId];
  if (!data) return;

  var storedTemp = data.temperature || data.base_temp || 75;
  var coolingRate = data.cooling_rate || 5.0;
  var refStr = data.last_accessed || data.created_at;
  var refMs = refStr ? new Date(refStr).getTime() : Date.now();

  function checkDecay() {
    // Skip if no longer in stream
    if (!streamCards[tokenId]) {
      clearInterval(streamDecayIntervals[tokenId]);
      delete streamDecayIntervals[tokenId];
      return;
    }

    var hoursElapsed = (Date.now() - refMs) / 3600000;
    var currentTemp = storedTemp - (coolingRate * hoursElapsed);
    currentTemp = Math.max(0, Math.min(100, currentTemp));

    if (currentTemp <= 0) {
      removeCueCardFromStream(tokenId);
      return;
    }

    // Scale opacity: 0.35 at full temp, proportional down
    var ratio = currentTemp / storedTemp;
    var opacity = Math.max(0.08, 0.35 * ratio);

    // For individual cards, adjust opacity and update age
    var card = document.querySelector("[data-stream-id=\"" + tokenId + "\"]");
    if (card && !card.closest(".cue-pile")) {
      card.style.opacity = opacity;
      var ageEl = card.querySelector(".cue-card__age");
      if (ageEl) {
        ageEl.textContent = formatStreamAge(data.created_at);
      }
    }
  }

  checkDecay();
  streamDecayIntervals[tokenId] = setInterval(checkDecay, 30000);
}

function removeCueCardFromStream(tokenId) {
  // Clean up decay interval
  if (streamDecayIntervals[tokenId]) {
    clearInterval(streamDecayIntervals[tokenId]);
    delete streamDecayIntervals[tokenId];
  }

  // Remove from any pile
  var trackNames = Object.keys(trackPiles);
  for (var i = 0; i < trackNames.length; i++) {
    var pile = trackPiles[trackNames[i]];
    var idx = pile.tokenIds.indexOf(tokenId);
    if (idx !== -1) {
      pile.tokenIds.splice(idx, 1);
      if (pile.tokenIds.length === 0) {
        // Remove empty pile
        if (pile.rotationInterval) clearInterval(pile.rotationInterval);
        if (pile.el && pile.el.parentNode) pile.el.parentNode.removeChild(pile.el);
        delete trackPiles[trackNames[i]];
      } else {
        updatePileDisplay(trackNames[i]);
      }
      delete streamCards[tokenId];
      return;
    }
  }

  // Remove individual card with fade animation
  var card = document.querySelector("[data-stream-id=\"" + tokenId + "\"]");
  if (card) {
    card.classList.add("cue-card--fading");
    setTimeout(function() {
      if (card.parentNode) card.parentNode.removeChild(card);
    }, 300);
  }
  delete streamCards[tokenId];
}

function pinFromStream(tokenId) {
  // Keep card in stream, additionally pin to shelf
  playSound("affirmative");
  // Create modifier (pin) via socket
  socket.emit("create_modifier", { token_id: tokenId });
  // Optimistically create thumbnail
  createModifierThumbnail(tokenId, null);
  // Update pin icon in conversation
  var card = conversation.querySelector("[data-token-id=\"" + tokenId + "\"]");
  if (card) {
    var modIcon = card.querySelector(".pin-icon");
    if (modIcon) modIcon.setAttribute("data-state", "active");
  }
}

// Stream positioning: pad stream top to clear pinned items
(function() {
  var pinnedEl = document.getElementById("pinnedTokens");
  var streamEl = document.getElementById("cueStream");
  if (!pinnedEl || !streamEl) return;

  function updatePosition() {
    var pinnedRect = pinnedEl.getBoundingClientRect();
    var bottom = pinnedRect.top + pinnedRect.height;
    streamEl.style.top = (bottom > 0 ? bottom + 8 : 0) + "px";
  }

  if (typeof ResizeObserver !== "undefined") {
    var ro = new ResizeObserver(updatePosition);
    ro.observe(pinnedEl);
  }

  window.addEventListener("resize", updatePosition);
  updatePosition();
  window._updateStreamPosition = updatePosition;
})();

// ============================================
// DROP VIEWER
// Drag images/videos anywhere on page to view via gallery lightbox
// ============================================
(function() {
  var conv = document.getElementById("conversation");
  if (!conv) {
    console.warn("[drop-viewer] no #conversation element found");
    return;
  }
  console.log("[drop-viewer] initialized on document.body, appending to #conversation");

  var _imageTypes = ["image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"];
  var _videoTypes = ["video/mp4", "video/quicktime", "video/webm", "video/x-m4v"];
  var _dragDepth = 0;

  function isMediaFile(file) {
    var hit = _imageTypes.indexOf(file.type) >= 0 || _videoTypes.indexOf(file.type) >= 0;
    console.log("[drop-viewer] file: " + file.name + " type: " + file.type + " accepted: " + hit);
    return hit;
  }

  var overlay = document.getElementById("dropOverlay");

  function showOverlay() {
    if (overlay) overlay.classList.add("drop-overlay--active");
  }
  function hideOverlay() {
    if (overlay) overlay.classList.remove("drop-overlay--active");
  }

  document.body.addEventListener("dragenter", function(e) {
    e.preventDefault();
    _dragDepth++;
    if (_dragDepth === 1) showOverlay();
  });

  document.body.addEventListener("dragleave", function(e) {
    e.preventDefault();
    _dragDepth--;
    if (_dragDepth <= 0) {
      _dragDepth = 0;
      hideOverlay();
    }
  });

  document.body.addEventListener("dragover", function(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  });

  document.body.addEventListener("drop", function(e) {
    e.preventDefault();
    _dragDepth = 0;
    hideOverlay();
    console.log("[drop-viewer] drop event fired");

    var files = e.dataTransfer && e.dataTransfer.files;
    if (!files || files.length === 0) {
      console.log("[drop-viewer] no files in drop");
      return;
    }
    console.log("[drop-viewer] " + files.length + " file(s) dropped");

    var mediaFiles = [];
    for (var i = 0; i < files.length; i++) {
      if (isMediaFile(files[i])) mediaFiles.push(files[i]);
    }
    if (mediaFiles.length === 0) {
      console.warn("[drop-viewer] no media files found in drop");
      return;
    }

    // Build gallery data from dropped files
    var images = [];
    for (var j = 0; j < mediaFiles.length; j++) {
      var file = mediaFiles[j];
      var blobUrl = URL.createObjectURL(file);
      console.log("[drop-viewer] blob URL: " + blobUrl);
      var item = { src: blobUrl, alt: file.name };
      if (_videoTypes.indexOf(file.type) >= 0) item.type = "video";
      images.push(item);
    }

    var strip = createGalleryStrip({ images: images });
    if (!strip) {
      console.warn("[drop-viewer] createGalleryStrip returned null");
      return;
    }

    // Wrap in a message card with processing status
    var card = document.createElement("article");
    card.className = "card assistant";
    card.dataset.timestamp = Date.now();

    var body = document.createElement("div");
    body.className = "card__body";

    body.appendChild(strip);
    card.appendChild(body);

    conv.appendChild(card);
    conv.scrollTop = conv.scrollHeight;

    // TTS confirmation
    var spoken = mediaFiles.length === 1 ? "image received" : "images received";
    socket.emit("speak", { text: spoken });
    console.log("[drop-viewer] " + spoken);

    // Get the gallery ID from the strip element
    var galleryId = strip.getAttribute("data-gallery-id");

    // Register each image: save, analyze, create token chain
    for (var k = 0; k < mediaFiles.length; k++) {
      (function(idx) {
        var fname = mediaFiles[idx].name;
        console.log("[drop-viewer] reading file " + (idx + 1) + "/" + mediaFiles.length + ": " + fname);
        var reader = new FileReader();
        reader.onload = function() {
          var b64 = reader.result;
          var sizeMB = (b64.length * 0.75 / 1024 / 1024).toFixed(2);
          console.log("[drop-viewer] sending " + fname + " to /api/drop-register (" + sizeMB + " MB)");
          fetch("/api/drop-register", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              image: b64,
              filename: fname
            })
          }).then(function(r) {
            console.log("[drop-viewer] response status: " + r.status);
            return r.ok ? r.json() : null;
          }).then(function(data) {
              if (!data) {
                console.warn("[drop-viewer] no data returned for " + fname);
                return;
              }
              var status = data.recognized ? "RECOGNIZED" : "NEW";
              console.log("[drop-viewer] === " + fname + " === " + status + " ===");
              if (data.recognized && data.vault) {
                console.log("[drop-viewer]   vault: " + data.vault.slug + "/" + data.vault.vault_filename);
                console.log("[drop-viewer]   port: " + data.vault.port);
                if (data.vault.blob_path) console.log("[drop-viewer]   blob: " + data.vault.blob_path);
              }
              console.log("[drop-viewer]   hash: " + data.file_hash.substring(0, 16));
              console.log("[drop-viewer]   tokens: drop=" + data.token1_id + " visual=" + data.token2_id + " context=" + data.token3_id);
              console.log("[drop-viewer]   caption: " + (data.caption || "(none)"));
              if (data.description) console.log("[drop-viewer]   description: " + data.description.substring(0, 100));
              if (data.ocr && data.ocr !== "none") console.log("[drop-viewer]   ocr: " + data.ocr.substring(0, 100));
              if (data.colors) console.log("[drop-viewer]   colors: " + data.colors);
              if (data.context_blurb) console.log("[drop-viewer]   context: " + data.context_blurb.substring(0, 100));
              console.log("[drop-viewer]   path: " + data.path);
              if (data.caption) {
                _persistCaption(galleryId, idx, data.caption);
              }
            }).catch(function(err) {
              console.error("[drop-viewer] register failed for " + fname + ":", err);
            });
        };
        reader.readAsDataURL(mediaFiles[idx]);
      })(k);
    }
  });
})();

console.log('CUE-VOX V2 initialized');

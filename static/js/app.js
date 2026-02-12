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

// Pinnable token state
var pinnedTokens = {};
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

    // Block if there's pending input
    if (hasPendingInput) {
      console.log('⚠️ Please answer the question first');
      return;
    }

    if (currentState === 'speaking') {
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
  socket.emit('interrupt');
});

drawerStopLink.addEventListener('click', (e) => {
  e.preventDefault();
  socket.emit('interrupt');
});

// ============================================
// Socket Events
// ============================================

socket.on('state_change', (data) => {
  console.log('🔄 State change:', data.state);
  setState(data.state);
});

socket.on('transcription', (data) => {
  console.log('🎤 Transcription received:', data.text);
  addMessage('user', data.text);
});

socket.on('response', (data) => {
  console.log('🤖 Response received:', data.text.substring(0, 50) + '...');
  addMessage('assistant', data.text);
});

socket.on('error', (data) => {
  console.error('❌ Socket error:', data.message);
  addSystemMessage('Error: ' + data.message);
  setState('idle');
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

function updateStatusTimer() {
  var label;
  if (currentState === "recording") {
    var remaining = RECORDING_LIMIT_MS - (Date.now() - stateStartTime);
    if (remaining < 0) remaining = 0;
    label = currentState + " " + formatElapsed(remaining);
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

function addMessage(role, text) {
  // Create a simple hash for deduplication
  const messageKey = `${role}:${text.substring(0, 50)}`;
  const now = Date.now();

  // Prevent adding the exact same message twice in a row within 1 second
  if (lastMessageHash) {
    const [lastKey, lastTime] = lastMessageHash.split('|');
    if (lastKey === messageKey && (now - parseInt(lastTime)) < 1000) {
      console.warn('⚠️ Duplicate message blocked:', text.substring(0, 50));
      return;
    }
  }
  lastMessageHash = `${messageKey}|${now}`;

  const messageCard = document.createElement('article');
  messageCard.className = `card ${role}`;
  messageCard.dataset.timestamp = Date.now();

  const header = document.createElement('header');
  header.className = 'card__header';

  const icon = document.createElement('span');
  icon.className = 'card__icon';
  icon.setAttribute('aria-hidden', 'true');
  icon.textContent = role === 'user' ? '👤' : '🤖';

  const title = document.createElement('h3');
  title.className = 'card__title';
  title.textContent = role === 'user' ? 'You' : 'Assistant';

  header.appendChild(icon);
  header.appendChild(title);

  const body = document.createElement('div');
  body.className = 'card__body';

  // Render message with embedded structured content
  renderMessageContent(body, text);

  // Add timestamp
  const timestamp = document.createElement('div');
  timestamp.className = 'message-timestamp';
  timestamp.textContent = 'just now';
  body.appendChild(timestamp);

  messageCard.appendChild(header);
  messageCard.appendChild(body);

  conversation.appendChild(messageCard);
  conversation.scrollTop = conversation.scrollHeight;
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
  }
};

// Sub-registry for INPUT type dispatching
const inputWidgetRegistry = {
  slider: function(inputData) {
    return createSemanticSlider(inputData);
  },
  text: function(inputData) {
    return createTextInput(inputData);
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

// Render message content with embedded structured components
function renderMessageContent(container, text) {
  console.log("Rendering message:", text.substring(0, 100) + (text.length > 100 ? "..." : ""));

  // Single master regex matches all structured tags
  var tagRegex = /\[(YES_NO|INPUT|APPROVAL|DOCUMENT|CUE):\s*([\s\S]+?)\]/g;

  var matches = [];
  var match;

  while ((match = tagRegex.exec(text)) !== null) {
    matches.push({
      type: match[1],
      index: match.index,
      length: match[0].length,
      data: match[2]
    });
  }

  // Sort by position
  matches.sort(function(a, b) { return a.index - b.index; });

  var lastIndex = 0;
  var hasContent = matches.length > 0;

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
        var widget = creator(m.data);
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
}

// Create pin icon for structured question cards
function createPinIcon() {
  var btn = document.createElement("button");
  btn.className = "pin-icon";
  btn.setAttribute("data-state", "inactive");
  btn.style.display = "none"; // hidden until token_created assigns an ID
  btn.setAttribute("aria-label", "Pin token");

  var glyph = document.createElement("span");
  glyph.className = "pin-icon__glyph";
  btn.appendChild(glyph);

  btn.addEventListener("click", function(e) {
    e.stopPropagation();
    var tokenId = btn.dataset.tokenId;
    if (!tokenId) return;

    var state = btn.getAttribute("data-state");
    if (state === "inactive" || state === "sleeping") {
      socket.emit("pin_token", { token_id: tokenId });
      btn.setAttribute("data-state", "active");
      var card = btn.closest(".structured-question");
      createPinnedThumbnail(tokenId, card);
    } else {
      socket.emit("unpin_token", { token_id: tokenId });
      btn.setAttribute("data-state", "inactive");
      removePinnedThumbnail(tokenId);
    }
  });

  return btn;
}

// Create YES/NO question UI
function createYesNoQuestion(questionText) {
  const container = document.createElement('div');
  container.className = 'structured-question';

  const question = document.createElement('p');
  question.className = 'card__description';
  question.textContent = questionText;
  container.appendChild(question);

  const buttonGroup = document.createElement('div');
  buttonGroup.className = 'button-group';
  buttonGroup.style.display = 'flex';
  buttonGroup.style.gap = 'var(--space-sm, 0.5rem)';
  buttonGroup.style.marginTop = 'var(--space-md, 0.75rem)';

  const yesBtn = document.createElement('button');
  yesBtn.className = 'btn btn--primary';
  yesBtn.textContent = 'Yes';
  yesBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    handleQuestionResponse('Yes', buttonGroup, questionText);
  });

  const noBtn = document.createElement('button');
  noBtn.className = 'btn btn--secondary';
  noBtn.textContent = 'No';
  noBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    handleQuestionResponse('No', buttonGroup, questionText);
  });

  buttonGroup.appendChild(yesBtn);
  buttonGroup.appendChild(noBtn);
  container.appendChild(buttonGroup);

  container.appendChild(createPinIcon());

  // Block other input when question is pending
  setPendingInput(true);

  return container;
}

// Handle question response
function handleQuestionResponse(answer, buttonGroup, questionText) {
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

  // Submit button
  var submitBtn = document.createElement("button");
  submitBtn.className = "btn btn--primary";
  submitBtn.textContent = "Submit";
  submitBtn.style.marginTop = "var(--space-md, 0.75rem)";
  submitBtn.addEventListener("click", function(e) {
    e.stopPropagation();
    var thermal = container.dataset.thermal ? JSON.parse(container.dataset.thermal) : null;
    handleSliderResponse(slider.value, slider, submitBtn, inputData.semantic_label, inputData.question, thermal);
  });

  container.appendChild(submitBtn);
  container.appendChild(createPinIcon());

  // Block other input when question is pending
  setPendingInput(true);

  return container;
}

// Handle slider response
function handleSliderResponse(value, slider, submitBtn, semanticLabel, question, thermal) {
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

  // Submit button
  var submitBtn = document.createElement("button");
  submitBtn.className = "btn btn--primary";
  submitBtn.textContent = "Submit";
  submitBtn.style.marginTop = "var(--space-md, 0.75rem)";
  submitBtn.addEventListener("click", function(e) {
    e.stopPropagation();
    var thermal = container.dataset.thermal ? JSON.parse(container.dataset.thermal) : null;
    handleTextResponse(textarea.value, textarea, submitBtn, inputData.semantic_label, inputData.question, thermal);
  });

  container.appendChild(submitBtn);
  container.appendChild(createPinIcon());

  // Block other input when question is pending
  setPendingInput(true);

  return container;
}

// Handle text input response
function handleTextResponse(value, textarea, submitBtn, semanticLabel, question, thermal) {
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

  container.appendChild(createPinIcon());

  // Block other input when approval is pending
  setPendingInput(true);

  return container;
}

// Handle approval response
function handleApprovalResponse(decision, buttonGroup, approvalData) {
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

  container.appendChild(createPinIcon());

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
// Pinnable Tokens - Token Created Listener
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
    created_at: data.created_at
  };

  // Find the most recent untagged structured-question in the conversation
  var questions = conversation.querySelectorAll(".structured-question:not([data-token-id])");
  if (questions.length > 0) {
    var lastQuestion = questions[questions.length - 1];
    lastQuestion.setAttribute("data-token-id", data.token_id);
    lastQuestion.setAttribute("data-token-type", data.type);
    lastQuestion.setAttribute("data-token-label", data.label);
    lastQuestion.setAttribute("data-token-value", data.value);

    // Show and wire up the pin icon
    var pinIcon = lastQuestion.querySelector(".pin-icon");
    if (pinIcon) {
      pinIcon.dataset.tokenId = data.token_id;
      pinIcon.setAttribute("data-state", "inactive");
      pinIcon.style.display = "flex";
    }
  }
});

// ============================================
// Pinnable Tokens - Thumbnail Management
// ============================================

function createPinnedThumbnail(tokenId, sourceCard) {
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
  label.textContent = data.label || data.key || "token";
  info.appendChild(label);

  var value = document.createElement("p");
  value.className = "pinned-thumbnail__value";
  value.textContent = data.value || "";
  info.appendChild(value);

  // Countdown timer
  var countdown = document.createElement("p");
  countdown.className = "pinned-thumbnail__countdown";
  countdown.textContent = "";
  info.appendChild(countdown);

  thumb.appendChild(info);

  // Single tap = renew pin, long press or double-tap would open lightbox
  // Using click for lightbox, dedicated renew button area
  thumb.addEventListener("click", function(e) {
    // If clicking the countdown area, renew the pin
    if (e.target === countdown || e.target.classList.contains("pinned-thumbnail__countdown")) {
      e.stopPropagation();
      socket.emit("renew_pin", { token_id: tokenId });
      return;
    }
    openLightbox(tokenId);
  });

  // Most recent pin goes to the top
  if (container.firstChild) {
    container.insertBefore(thumb, container.firstChild);
  } else {
    container.appendChild(thumb);
  }
  pinnedTokens[tokenId] = thumb;

  // Start countdown if we have pinned_at
  if (data.pinned_at) {
    startPinCountdown(tokenId, data.pinned_at);
  }
}

// Move a pinned thumbnail to the top of the list with FLIP animation
function promotePinnedThumbnail(tokenId) {
  var thumb = pinnedTokens[tokenId];
  if (!thumb || !thumb.parentNode) return;
  var container = thumb.parentNode;
  if (container.firstChild === thumb) return;

  // FLIP: capture old positions for all siblings
  var children = Array.prototype.slice.call(container.children);
  var firstRects = {};
  for (var i = 0; i < children.length; i++) {
    var id = children[i].getAttribute("data-token-id");
    if (id) firstRects[id] = children[i].getBoundingClientRect();
  }

  // Move the DOM node
  container.insertBefore(thumb, container.firstChild);

  // FLIP: compute deltas and animate each child
  var moved = Array.prototype.slice.call(container.children);
  for (var j = 0; j < moved.length; j++) {
    var child = moved[j];
    var cid = child.getAttribute("data-token-id");
    if (!cid || !firstRects[cid]) continue;
    var lastRect = child.getBoundingClientRect();
    var dy = firstRects[cid].top - lastRect.top;
    if (dy === 0) continue;
    child.style.transform = "translateY(" + dy + "px)";
    child.style.transition = "none";
  }

  // Force reflow then play
  container.offsetHeight;
  for (var k = 0; k < moved.length; k++) {
    moved[k].style.transition = "";
    moved[k].style.transform = "";
  }
}

// Pin countdown tracking
var pinCountdownIntervals = {};

function startPinCountdown(tokenId, pinnedAt) {
  // Clear any existing interval
  if (pinCountdownIntervals[tokenId]) {
    clearInterval(pinCountdownIntervals[tokenId]);
  }

  var pinnedTime = new Date(pinnedAt + "Z").getTime();
  var ttlMs = 2 * 60 * 60 * 1000; // 2 hours

  function updateCountdown() {
    var now = Date.now();
    var elapsed = now - pinnedTime;
    var remaining = ttlMs - elapsed;

    var thumb = pinnedTokens[tokenId];
    if (!thumb) {
      clearInterval(pinCountdownIntervals[tokenId]);
      delete pinCountdownIntervals[tokenId];
      return;
    }

    var countdownEl = thumb.querySelector(".pinned-thumbnail__countdown");
    if (!countdownEl) return;

    if (remaining <= 0) {
      // Pin expired — remove thumbnail
      removePinnedThumbnail(tokenId);
      clearInterval(pinCountdownIntervals[tokenId]);
      delete pinCountdownIntervals[tokenId];
      // Reset pin icon on source card
      var card = conversation.querySelector("[data-token-id=\"" + tokenId + "\"]");
      if (card) {
        var pinIcon = card.querySelector(".pin-icon");
        if (pinIcon) pinIcon.setAttribute("data-state", "inactive");
      }
      return;
    }

    var mins = Math.floor(remaining / 60000);
    var hours = Math.floor(mins / 60);
    mins = mins % 60;

    if (hours > 0) {
      countdownEl.textContent = hours + "h " + mins + "m";
    } else {
      countdownEl.textContent = mins + "m";
    }

    // Visual fade as time runs low (last 15 minutes)
    if (remaining < 15 * 60 * 1000) {
      thumb.style.opacity = "0.5";
      countdownEl.style.color = "var(--state-recording, #ff4444)";
    } else {
      thumb.style.opacity = "";
      countdownEl.style.color = "";
    }
  }

  updateCountdown();
  pinCountdownIntervals[tokenId] = setInterval(updateCountdown, 30000); // Update every 30s
}

function removePinnedThumbnail(tokenId) {
  var thumb = pinnedTokens[tokenId];
  if (thumb && thumb.parentNode) {
    thumb.parentNode.removeChild(thumb);
  }
  delete pinnedTokens[tokenId];
  // Clean up countdown interval
  if (pinCountdownIntervals[tokenId]) {
    clearInterval(pinCountdownIntervals[tokenId]);
    delete pinCountdownIntervals[tokenId];
  }
}

// ============================================
// Pinnable Tokens - Lightbox Modal
// ============================================

function openLightbox(tokenId) {
  var data = tokenRegistry[tokenId];
  if (!data) return;

  currentLightboxTokenId = tokenId;

  var lightbox = document.getElementById("lightbox");
  var title = document.getElementById("lightboxTitle");
  var body = document.getElementById("lightboxBody");

  title.textContent = data.label || data.key || "Token";
  renderLightboxBody(body, data, false);

  // Show view mode buttons, hide edit mode buttons
  document.getElementById("lightboxEditBtn").style.display = "";
  document.getElementById("lightboxDeleteBtn").style.display = "";
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
    lines.push("dimension   " + (data.label || "param") + " @ " + sv + "/100");
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
  if (data.pinned_at) {
    lines.push("pinned      " + data.pinned_at.replace("T", " ").substring(0, 19));
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
  var deleteBtn = document.getElementById("lightboxDeleteBtn");
  var unpinBtn = document.getElementById("lightboxUnpinBtn");
  var saveBtn = document.getElementById("lightboxSaveBtn");
  var cancelBtn = document.getElementById("lightboxCancelBtn");
  var closeBtn = document.getElementById("lightboxClose");
  var backdrop = document.getElementById("lightboxBackdrop");

  unpinBtn.addEventListener("click", function() {
    if (!currentLightboxTokenId) return;
    var tokenId = currentLightboxTokenId;
    socket.emit("unpin_token", { token_id: tokenId });
    removePinnedThumbnail(tokenId);
    // Reset pin icon on the source card
    var card = conversation.querySelector("[data-token-id=\"" + tokenId + "\"]");
    if (card) {
      var pinIcon = card.querySelector(".pin-icon");
      if (pinIcon) {
        pinIcon.setAttribute("data-state", "inactive");
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
    deleteBtn.style.display = "none";
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
    deleteBtn.style.display = "";
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
    promotePinnedThumbnail(currentLightboxTokenId);
    closeLightbox();
  });

  deleteBtn.addEventListener("click", function() {
    if (!currentLightboxTokenId) return;
    var tokenId = currentLightboxTokenId;
    socket.emit("delete_token", { token_id: tokenId });
    removePinnedThumbnail(tokenId);
    // Reset pin icon on the source card
    var card = conversation.querySelector("[data-token-id=\"" + tokenId + "\"]");
    if (card) {
      var pinIcon = card.querySelector(".pin-icon");
      if (pinIcon) {
        pinIcon.setAttribute("data-state", "sleeping");
      }
    }
    delete tokenRegistry[tokenId];
    closeLightbox();
  });

  closeBtn.addEventListener("click", closeLightbox);
  backdrop.addEventListener("click", closeLightbox);
})();

function updateThumbnailValue(tokenId, newValue) {
  var thumb = pinnedTokens[tokenId];
  if (!thumb) return;
  var valueEl = thumb.querySelector(".pinned-thumbnail__value");
  if (valueEl) {
    valueEl.textContent = newValue;
  }
}

// ============================================
// Pinnable Tokens - Socket Confirmations
// ============================================

socket.on("token_pinned", function(data) {
  console.log("Token pinned confirmed:", data.token_id);
  if (data.pinned_at && tokenRegistry[data.token_id]) {
    tokenRegistry[data.token_id].pinned_at = data.pinned_at;
    startPinCountdown(data.token_id, data.pinned_at);
  }
});

socket.on("token_unpinned", function(data) {
  console.log("Token unpinned confirmed:", data.token_id);
  if (pinCountdownIntervals[data.token_id]) {
    clearInterval(pinCountdownIntervals[data.token_id]);
    delete pinCountdownIntervals[data.token_id];
  }
});

socket.on("pin_renewed", function(data) {
  console.log("Pin renewed:", data.token_id, data.pinned_at);
  if (tokenRegistry[data.token_id]) {
    tokenRegistry[data.token_id].pinned_at = data.pinned_at;
  }
  startPinCountdown(data.token_id, data.pinned_at);
  promotePinnedThumbnail(data.token_id);
});

// Hydrate pinned tokens on page load (most recent first)
socket.on("hydrate_pins", function(data) {
  var pins = data.pins || [];
  console.log("Hydrating %d pinned tokens", pins.length);

  // Sort by pinned_at descending so most recent renders at top
  pins.sort(function(a, b) {
    var ta = a.pinned_at ? new Date(a.pinned_at + "Z").getTime() : 0;
    var tb = b.pinned_at ? new Date(b.pinned_at + "Z").getTime() : 0;
    return tb - ta;
  });

  for (var i = 0; i < pins.length; i++) {
    var pin = pins[i];
    // Register in tokenRegistry if not already there
    if (!tokenRegistry[pin.token_id]) {
      tokenRegistry[pin.token_id] = pin;
    }
    // Create thumbnail with countdown
    createPinnedThumbnail(pin.token_id, null);
  }
});

socket.on("token_updated", function(data) {
  console.log("Token updated confirmed:", data.token_id, data.new_value);
  if (tokenRegistry[data.token_id]) {
    tokenRegistry[data.token_id].value = data.new_value;
  }
  updateThumbnailValue(data.token_id, data.new_value);
  promotePinnedThumbnail(data.token_id);
});

socket.on("token_deleted", function(data) {
  console.log("Token deleted confirmed:", data.token_id);
  removePinnedThumbnail(data.token_id);
  var card = conversation.querySelector("[data-token-id=\"" + data.token_id + "\"]");
  if (card) {
    var pinIcon = card.querySelector(".pin-icon");
    if (pinIcon) {
      pinIcon.setAttribute("data-state", "sleeping");
    }
  }
  delete tokenRegistry[data.token_id];
});

// ============================================
// Initialize
// ============================================

setState('idle');
initAudio();

console.log('✅ CUE-VOX V2 initialized');

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
  }
});

document.addEventListener('keyup', (e) => {
  // Don't trigger if typing in any text input or textarea
  if (e.target === drawerTextInput || e.target.matches('textarea, input[type="text"]')) return;

  if (e.code === 'Space' && isRecording) {
    e.preventDefault();
    console.log('⏹️ Stopping recording...');
    isRecording = false;
    setState('transcribing');

    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
      mediaRecorder.stop();
    } else {
      console.error('❌ MediaRecorder not active');
    }
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

function setState(state) {
  currentState = state;

  // Update state dot
  if (stateDot) {
    stateDot.setAttribute('data-state', state);
  }

  // Update drawer status
  if (drawerStatusDot) {
    drawerStatusDot.setAttribute('data-state', state);
  }

  if (drawerStatusText) {
    drawerStatusText.textContent = state;
  }

  // Update canvas status
  if (canvasStatus) {
    canvasStatus.textContent = state;

    if (state === 'recording' || state === 'transcribing' || state === 'thinking') {
      canvasStatus.classList.add('active');
    } else {
      canvasStatus.classList.remove('active');
    }
  }

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

// Render message content with embedded structured components
function renderMessageContent(container, text) {
  console.log('📝 Rendering message:', text.substring(0, 100) + (text.length > 100 ? '...' : ''));
  console.log('   Container children before:', container.children.length);

  // Find all structured tags (YES_NO and INPUT) and sort by position
  const yesNoRegex = /\[YES_NO:\s*(.+?)\]/g;
  const inputRegex = /\[INPUT:\s*(\{[\s\S]+?\})\]/g;

  const matches = [];
  let match;

  // Collect all YES_NO matches
  while ((match = yesNoRegex.exec(text)) !== null) {
    matches.push({
      type: 'YES_NO',
      index: match.index,
      length: match[0].length,
      data: match[1]
    });
  }

  // Collect all INPUT matches
  while ((match = inputRegex.exec(text)) !== null) {
    matches.push({
      type: 'INPUT',
      index: match.index,
      length: match[0].length,
      data: match[1]
    });
  }

  // Sort by position
  matches.sort((a, b) => a.index - b.index);

  let lastIndex = 0;
  let hasContent = matches.length > 0;

  // Process each match in order
  matches.forEach((match, i) => {
    console.log(`✅ Detected ${match.type} tag at position ${match.index}`);

    // Add text before the tag
    const textBefore = text.substring(lastIndex, match.index).trim();
    if (textBefore) {
      console.log('  → Adding text before:', textBefore.substring(0, 50));
      const p = document.createElement('p');
      p.className = 'card__description';
      p.textContent = textBefore;
      container.appendChild(p);
    }

    // Add the structured component
    if (match.type === 'YES_NO') {
      console.log('  → Creating YES/NO question:', match.data);
      container.appendChild(createYesNoQuestion(match.data));
    } else if (match.type === 'INPUT') {
      try {
        const inputData = JSON.parse(match.data);
        console.log('  → Parsed INPUT data:', inputData);

        if (inputData.type === 'slider') {
          console.log('  → Creating slider:', inputData.question);
          container.appendChild(createSemanticSlider(inputData));
        } else if (inputData.type === 'text') {
          console.log('  → Creating text input:', inputData.question);
          container.appendChild(createTextInput(inputData));
        } else {
          console.warn('  ⚠️ Unknown input type:', inputData.type);
        }
      } catch (e) {
        console.error('❌ Failed to parse INPUT JSON:', e);
        console.error('   Raw JSON string:', match.data);
        const p = document.createElement('p');
        p.className = 'card__description';
        p.textContent = `[INPUT: ${match.data}]`;
        p.style.color = 'var(--error, #ff4444)';
        container.appendChild(p);
      }
    }

    lastIndex = match.index + match.length;
  });

  // If no structured content was found, just render as plain text
  if (!hasContent) {
    // Plain text message
    console.log('📄 Plain text message');
    const p = document.createElement('p');
    p.className = 'card__description';
    p.textContent = text;
    container.appendChild(p);
  } else {
    // Add any remaining text after the last tag
    const textAfter = text.substring(lastIndex).trim();
    if (textAfter) {
      console.log('  → Adding text after:', textAfter.substring(0, 50));
      const p = document.createElement('p');
      p.className = 'card__description';
      p.textContent = textAfter;
      container.appendChild(p);
    }
  }

  console.log('   Container children after:', container.children.length);
  console.log('   ---');
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

  // Block other input when question is pending
  setPendingInput(true);

  return container;
}

// Handle question response
function handleQuestionResponse(answer, buttonGroup, questionText) {
  // Send the response with question context
  const response = `[Response to "${questionText}"]: ${answer}`;
  socket.emit('text_message', { text: response });

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
  addMessage('user', response);
}

// Create semantic slider (from JSON INPUT)
function createSemanticSlider(inputData) {
  const container = document.createElement('div');
  container.className = 'structured-question';

  const question = document.createElement('p');
  question.className = 'card__description';
  question.textContent = inputData.question;
  container.appendChild(question);

  // Slider container
  const sliderContainer = document.createElement('div');
  sliderContainer.className = 'slider-container';
  sliderContainer.style.marginTop = 'var(--space-md, 0.75rem)';

  // Scale labels (if provided)
  if (inputData.scale) {
    const lowLabel = document.createElement('div');
    lowLabel.className = 'slider-label slider-label--low';
    lowLabel.textContent = inputData.scale.low || 'Low';

    const highLabel = document.createElement('div');
    highLabel.className = 'slider-label slider-label--high';
    highLabel.textContent = inputData.scale.high || 'High';

    sliderContainer.appendChild(lowLabel);
  }

  // Slider input (0-100 scale for semantic sliders)
  const slider = document.createElement('input');
  slider.type = 'range';
  slider.min = 0;
  slider.max = 100;
  slider.step = 1;
  slider.value = 50; // Start at middle
  slider.className = 'slider-input';
  slider.dataset.semanticLabel = inputData.semantic_label || '';

  sliderContainer.appendChild(slider);

  if (inputData.scale) {
    const highLabel = document.createElement('div');
    highLabel.className = 'slider-label slider-label--high';
    highLabel.textContent = inputData.scale.high || 'High';
    sliderContainer.appendChild(highLabel);
  }

  container.appendChild(sliderContainer);

  // Submit button
  const submitBtn = document.createElement('button');
  submitBtn.className = 'btn btn--primary';
  submitBtn.textContent = 'Submit';
  submitBtn.style.marginTop = 'var(--space-md, 0.75rem)';
  submitBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    handleSliderResponse(slider.value, slider, submitBtn, inputData.semantic_label, inputData.question);
  });

  container.appendChild(submitBtn);

  // Block other input when question is pending
  setPendingInput(true);

  return container;
}

// Handle slider response
function handleSliderResponse(value, slider, submitBtn, semanticLabel, question) {
  console.log('Slider response:', value, 'Label:', semanticLabel);

  // Send the response with context
  let response;
  if (semanticLabel) {
    response = `${semanticLabel}: ${value}`;
  } else if (question) {
    response = `[Response to "${question}"]: ${value}`;
  } else {
    response = String(value);
  }
  console.log('Sending response:', response);

  socket.emit('text_message', { text: response });

  // Disable slider
  slider.disabled = true;
  slider.classList.add('disabled');

  // Remove submit button
  submitBtn.remove();

  // Add choice indicator
  const choiceIndicator = document.createElement('div');
  choiceIndicator.className = 'choice-indicator';
  choiceIndicator.textContent = `Selected: ${value}%`;

  slider.parentNode.parentNode.appendChild(choiceIndicator);

  // Unblock input
  setPendingInput(false);

  // Add user message immediately (backend doesn't echo slider responses)
  addMessage('user', response);
}

// Create text input (from JSON INPUT)
function createTextInput(inputData) {
  const container = document.createElement('div');
  container.className = 'structured-question';

  const question = document.createElement('p');
  question.className = 'card__description';
  question.textContent = inputData.question;
  container.appendChild(question);

  // Textarea container
  const textareaContainer = document.createElement('div');
  textareaContainer.className = 'text-input-container';
  textareaContainer.style.marginTop = 'var(--space-md, 0.75rem)';

  // Textarea input
  const textarea = document.createElement('textarea');
  textarea.className = 'text-input';
  textarea.placeholder = inputData.placeholder || 'Enter your response...';
  textarea.rows = inputData.rows || 4;
  textarea.dataset.semanticLabel = inputData.semantic_label || '';

  textareaContainer.appendChild(textarea);
  container.appendChild(textareaContainer);

  // Submit button
  const submitBtn = document.createElement('button');
  submitBtn.className = 'btn btn--primary';
  submitBtn.textContent = 'Submit';
  submitBtn.style.marginTop = 'var(--space-md, 0.75rem)';
  submitBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    handleTextResponse(textarea.value, textarea, submitBtn, inputData.semantic_label, inputData.question);
  });

  container.appendChild(submitBtn);

  // Block other input when question is pending
  setPendingInput(true);

  return container;
}

// Handle text input response
function handleTextResponse(value, textarea, submitBtn, semanticLabel, question) {
  console.log('Text input response:', value, 'Label:', semanticLabel);

  // Send the response with question context for better AI understanding
  let response;
  if (semanticLabel) {
    response = `${semanticLabel}: ${value}`;
  } else if (question) {
    response = `[Response to "${question}"]: ${value}`;
  } else {
    response = value;
  }
  console.log('Sending response:', response);

  socket.emit('text_message', { text: response });

  // Disable textarea
  textarea.disabled = true;
  textarea.classList.add('disabled');

  // Remove submit button
  submitBtn.remove();

  // Add submitted text indicator (truncated at 2 lines)
  const choiceIndicator = document.createElement('div');
  choiceIndicator.className = 'choice-indicator choice-indicator--text';

  const label = document.createElement('strong');
  label.textContent = 'Submitted: ';
  choiceIndicator.appendChild(label);

  const textSpan = document.createElement('span');
  textSpan.className = 'submitted-text';
  textSpan.textContent = value;
  choiceIndicator.appendChild(textSpan);

  textarea.parentNode.parentNode.appendChild(choiceIndicator);

  // Unblock input
  setPendingInput(false);

  // Add user message immediately (backend doesn't echo text input responses)
  addMessage('user', response);
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
  addMessage('user', text);
  socket.emit('text_message', { text: text });
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
// Initialize
// ============================================

setState('idle');
initAudio();

console.log('✅ CUE-VOX V2 initialized');

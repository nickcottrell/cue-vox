// ============================================
// CUE-VOX APPLICATION
// Voice-activated AI conversation interface
// ============================================

// DOM Elements
const socket = io();
const dot = document.getElementById('dot');
const conversation = document.getElementById('conversation');
const stopButton = document.getElementById('stopButton');
const drawer = document.getElementById('drawer');
const drawerToggle = document.getElementById('drawerToggle');
const drawerTextInput = document.getElementById('drawerTextInput');
const drawerSendButton = document.getElementById('drawerSendButton');
const drawerOverlay = document.getElementById('drawerOverlay');
const miniStatusDot = document.getElementById('miniStatusDot');
const miniStatusText = document.getElementById('miniStatusText');
const miniStopLink = document.getElementById('miniStopLink');
const statusText = document.getElementById('statusText');
const gateIndicator = document.getElementById('gateIndicator');

// State Variables
let mediaRecorder;
let audioChunks = [];
let isRecording = false;
let currentState = 'idle';
let yesNoQuestionPending = false;
let approvalPending = false;
let sessionStart = Date.now();
let recordingTimer = null;
let recordingStartTime = 0;
let activityTimer = null;
let activityStartTime = 0;
let estimatedTokens = 0;
const MAX_RECORDING_SECONDS = 30;

// ============================================
// Drawer Controls
// ============================================

drawerToggle.addEventListener('click', () => {
    drawer.classList.toggle('open');
});

drawerOverlay.addEventListener('click', () => {
    drawer.classList.remove('open');
});

// ============================================
// Status Management
// ============================================

function updateStatusText(state, extraText = '') {
    const statusMessages = {
        'recording': 'listening...' + (extraText ? ' ' + extraText : ''),
        'transcribing': 'transcribing audio...',
        'thinking': 'thinking...' + (extraText ? ' ' + extraText : ''),
        'speaking': 'speaking...',
        'idle': 'idle' + (extraText ? ' ' + extraText : ''),
        'waiting for button': 'click button to respond'
    };
    statusText.textContent = statusMessages[state] || '';

    if (state === 'recording' || state === 'transcribing' || state === 'thinking') {
        statusText.classList.add('active');
    } else {
        statusText.classList.remove('active');
    }
}

function updateSendButtonState() {
    if (yesNoQuestionPending || approvalPending) {
        drawerSendButton.classList.add('disabled');
        gateIndicator.classList.add('active');
    } else {
        drawerSendButton.classList.remove('disabled');
        gateIndicator.classList.remove('active');
    }
}

// ============================================
// Utility Functions
// ============================================

function getRelativeTime(timestamp) {
    const seconds = Math.floor((Date.now() - timestamp) / 1000);
    if (seconds < 60) return 'just now';

    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;

    const hours = Math.floor(minutes / 60);
    const remainingMinutes = minutes % 60;
    if (hours < 24) {
        return remainingMinutes > 0 ? `${hours}h ${remainingMinutes}m ago` : `${hours}h ago`;
    }

    const days = Math.floor(hours / 24);
    return `${days}d ago`;
}

function hslToHex(h, s, l) {
    s = s / 100;
    l = l / 100;
    const c = (1 - Math.abs(2 * l - 1)) * s;
    const x = c * (1 - Math.abs((h / 60) % 2 - 1));
    const m = l - c / 2;

    let r, g, b;
    if (0 <= h && h < 60) {
        r = c; g = x; b = 0;
    } else if (60 <= h && h < 120) {
        r = x; g = c; b = 0;
    } else if (120 <= h && h < 180) {
        r = 0; g = c; b = x;
    } else if (180 <= h && h < 240) {
        r = 0; g = x; b = c;
    } else if (240 <= h && h < 300) {
        r = x; g = 0; b = c;
    } else {
        r = c; g = 0; b = x;
    }

    r = Math.round((r + m) * 255);
    g = Math.round((g + m) * 255);
    b = Math.round((b + m) * 255);

    return `#${r.toString(16).padStart(2, '0')}${g.toString(16).padStart(2, '0')}${b.toString(16).padStart(2, '0')}`;
}

function interpretConfidence(h, s, l) {
    let domain, conviction, clarity;

    if (0 <= h && h < 60) {
        domain = 'urgent/time-sensitive';
    } else if (60 <= h && h < 120) {
        domain = 'creative/experimental';
    } else if (120 <= h && h < 180) {
        domain = 'safe/approved-pattern';
    } else if (180 <= h && h < 240) {
        domain = 'data-driven/analytical';
    } else if (240 <= h && h < 300) {
        domain = 'strategic/long-term';
    } else {
        domain = 'edge-case/exception';
    }

    if (s > 75) {
        conviction = 'very strong';
    } else if (s > 50) {
        conviction = 'moderate';
    } else if (s > 25) {
        conviction = 'weak';
    } else {
        conviction = 'uncertain';
    }

    if (l > 70) {
        clarity = 'very clear';
    } else if (l > 50) {
        clarity = 'moderately clear';
    } else if (l > 30) {
        clarity = 'somewhat unclear';
    } else {
        clarity = 'very uncertain';
    }

    return { domain, conviction, clarity };
}

function markdownToHtml(text) {
    let html = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    html = html.replace(/```([^`]+)```/g, '<pre><code>$1</code></pre>');
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/__([^_]+)__/g, '<strong>$1</strong>');
    html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    html = html.replace(/_([^_]+)_/g, '<em>$1</em>');
    html = html.replace(/\n/g, '<br>');

    return html;
}

// ============================================
// Text Input Handlers
// ============================================

function sendDrawerTextMessage() {
    if (yesNoQuestionPending || approvalPending) {
        updateStatusText('waiting for button');
        setTimeout(() => updateStatusText(currentState), 1500);
        return;
    }

    const text = drawerTextInput.value.trim();
    if (text) {
        addMessage('user', text);
        socket.emit('text_message', { text: text });
        drawerTextInput.value = '';
    }
}

drawerSendButton.addEventListener('click', sendDrawerTextMessage);

drawerTextInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') {
        e.preventDefault();
        sendDrawerTextMessage();
    }
});

// ============================================
// Stop Button Handlers
// ============================================

stopButton.addEventListener('click', () => {
    socket.emit('interrupt');
    stopButton.classList.remove('visible');
    miniStopLink.classList.remove('visible');
});

miniStopLink.addEventListener('click', (e) => {
    e.preventDefault();
    socket.emit('interrupt');
    miniStopLink.classList.remove('visible');
    stopButton.classList.remove('visible');
});

// ============================================
// Audio Recording
// ============================================

async function initAudio() {
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
}

// ============================================
// Keyboard Events
// ============================================

document.addEventListener('keydown', (e) => {
    if (e.target === drawerTextInput) {
        return;
    }

    if (e.code === 'Space' && !isRecording) {
        e.preventDefault();

        if (yesNoQuestionPending || approvalPending) {
            updateStatusText('waiting for button');
            setTimeout(() => updateStatusText(currentState), 1500);
            return;
        }

        if (currentState === 'speaking') {
            socket.emit('interrupt');
            return;
        }

        isRecording = true;
        recordingStartTime = Date.now();
        setState('recording');
        mediaRecorder.start(1000);

        recordingTimer = setInterval(() => {
            const elapsed = Math.floor((Date.now() - recordingStartTime) / 1000);
            const remaining = MAX_RECORDING_SECONDS - elapsed;

            if (remaining < 1) {
                clearInterval(recordingTimer);
                isRecording = false;
                mediaRecorder.stop();
                return;
            }

            updateStatusText('recording', `(${remaining}s)`);
        }, 100);
    }
});

document.addEventListener('keyup', (e) => {
    if (e.target === drawerTextInput) {
        return;
    }

    if (e.code === 'Space' && isRecording) {
        e.preventDefault();
        isRecording = false;
        if (recordingTimer) {
            clearInterval(recordingTimer);
            recordingTimer = null;
        }
        mediaRecorder.stop();
    }
});

// ============================================
// Socket Events
// ============================================

socket.on('state_change', (data) => {
    setState(data.state);
});

socket.on('transcription', (data) => {
    addMessage('user', data.text);
});

socket.on('response', (data) => {
    addMessage('assistant', data.text, data.yes_no);
});

socket.on('error', (data) => {
    console.error(data.message);
    setState('idle');
});

// ============================================
// Message Rendering
// ============================================

function addMessage(role, text, yesNoQuestion = false) {
    const message = document.createElement('div');
    message.className = `message ${role}`;
    const timestamp = Date.now();
    message.dataset.timestamp = timestamp;

    const contentDiv = document.createElement('div');

    function extractNestedJson(text, pattern) {
        const startMatch = text.match(pattern);
        if (!startMatch) return null;

        const startIndex = startMatch.index + startMatch[0].indexOf('{');
        let braceCount = 0;
        let endIndex = -1;

        for (let i = startIndex; i < text.length; i++) {
            if (text[i] === '{') braceCount++;
            if (text[i] === '}') {
                braceCount--;
                if (braceCount === 0) {
                    endIndex = i;
                    break;
                }
            }
        }

        if (endIndex === -1) return null;

        return {
            json: text.substring(startIndex, endIndex + 1),
            fullMatch: text.substring(startMatch.index, endIndex + 2),
            leadingText: text.substring(0, startMatch.index).trim(),
            trailingText: text.substring(endIndex + 2).trim()
        };
    }

    const inputExtract = extractNestedJson(text, /\[INPUT:\s*\{/);

    if (inputExtract) {
        try {
            const normalizedJson = inputExtract.json
                .replace(/[\u201C\u201D]/g, '"')
                .replace(/[\u2018\u2019]/g, "'");

            const inputData = JSON.parse(normalizedJson);
            const cleanText = (inputExtract.leadingText + ' ' + inputExtract.trailingText).trim();

            if (cleanText) {
                contentDiv.innerHTML += markdownToHtml(cleanText) + '<br>';
            }

            renderInputCard(contentDiv, inputData, message);

            approvalPending = true;
            updateStatusText('waiting for input');
            updateSendButtonState();
        } catch (e) {
            console.error('Failed to parse INPUT JSON:', e);
            contentDiv.innerHTML = markdownToHtml(text);
        }
    } else {
        const approvalExtract = extractNestedJson(text, /\[APPROVAL:\s*\{/);
        if (approvalExtract) {
            try {
                const approvalData = JSON.parse(approvalExtract.json);
                const cleanText = (approvalExtract.leadingText + ' ' + approvalExtract.trailingText).trim();

                if (cleanText) {
                    contentDiv.innerHTML += markdownToHtml(cleanText) + '<br>';
                }

                renderApprovalCard(contentDiv, approvalData, message);

                approvalPending = true;
                updateStatusText('waiting for approval');
                updateSendButtonState();
            } catch (e) {
                console.error('Failed to parse APPROVAL JSON:', e);
                contentDiv.innerHTML = markdownToHtml(text);
            }
        } else {
            const yesNoMatch = text.match(/\[YES_NO:\s*(.+?)\]/);

            if (yesNoMatch || yesNoQuestion) {
                const questionText = yesNoMatch ? yesNoMatch[1] : text;
                const cleanText = yesNoMatch ? text.replace(/\[YES_NO:\s*.+?\]/, '').trim() : '';

                if (cleanText) {
                    contentDiv.innerHTML += markdownToHtml(cleanText) + '<br>';
                }

                renderYesNoCard(contentDiv, questionText, message);

                yesNoQuestionPending = true;
                updateStatusText('waiting for button');
                updateSendButtonState();
            } else {
                contentDiv.innerHTML = markdownToHtml(text);

                if (role === 'assistant' && yesNoQuestionPending) {
                    yesNoQuestionPending = false;
                    updateStatusText('idle');
                    updateSendButtonState();
                }
            }
        }
    }

    message.appendChild(contentDiv);

    const timestampDiv = document.createElement('div');
    timestampDiv.className = 'message-timestamp';
    timestampDiv.textContent = getRelativeTime(timestamp);
    message.appendChild(timestampDiv);

    conversation.appendChild(message);
    conversation.scrollTop = conversation.scrollHeight;
}

// ============================================
// Card Rendering Functions
// ============================================

function renderApprovalCard(contentDiv, approvalData, messageEl) {
    const card = document.createElement('div');
    card.className = `card approval-card action-${approvalData.action || 'Unknown'}`;

    const header = document.createElement('div');
    header.className = 'card__header';

    const icon = document.createElement('span');
    icon.className = 'card__icon';
    icon.textContent = '🔐';
    header.appendChild(icon);

    const title = document.createElement('h3');
    title.className = 'card__title';
    title.textContent = `${approvalData.action || 'Action'} Request`;
    header.appendChild(title);

    card.appendChild(header);

    const body = document.createElement('div');
    body.className = 'card__body';

    if (approvalData.target) {
        const target = document.createElement('div');
        target.className = 'card__description approval-target';
        target.textContent = approvalData.target;
        body.appendChild(target);
    }

    if (approvalData.description) {
        const desc = document.createElement('p');
        desc.className = 'card__description';
        desc.textContent = approvalData.description;
        body.appendChild(desc);
    }

    if (approvalData.preview) {
        const previewContainer = document.createElement('div');
        previewContainer.className = 'approval-preview collapsed';
        const pre = document.createElement('pre');
        pre.textContent = approvalData.preview;
        previewContainer.appendChild(pre);
        body.appendChild(previewContainer);

        const toggle = document.createElement('a');
        toggle.className = 'approval-toggle';
        toggle.textContent = 'Show full preview';
        toggle.onclick = () => {
            if (previewContainer.classList.contains('collapsed')) {
                previewContainer.classList.remove('collapsed');
                toggle.textContent = 'Hide preview';
            } else {
                previewContainer.classList.add('collapsed');
                toggle.textContent = 'Show full preview';
            }
        };
        body.appendChild(toggle);
    }

    const confidenceSection = renderConfidenceControls(card);
    body.appendChild(confidenceSection);

    const confToggle = document.createElement('a');
    confToggle.className = 'confidence-toggle';
    confToggle.textContent = 'Adjust confidence';
    confToggle.onclick = () => {
        if (confidenceSection.classList.contains('collapsed')) {
            confidenceSection.classList.remove('collapsed');
            confToggle.textContent = 'Hide confidence';
        } else {
            confidenceSection.classList.add('collapsed');
            confToggle.textContent = 'Adjust confidence';
        }
    };
    body.appendChild(confToggle);

    card.appendChild(body);

    const actions = document.createElement('div');
    actions.className = 'card__actions';

    const approveBtn = document.createElement('button');
    approveBtn.className = 'btn btn--success';
    approveBtn.textContent = 'Approve';
    approveBtn.onclick = () => handleApprovalClick(messageEl, 'Approve', approvalData, actions, confidenceSection);

    const denyBtn = document.createElement('button');
    denyBtn.className = 'btn btn--error';
    denyBtn.textContent = 'Deny';
    denyBtn.onclick = () => handleApprovalClick(messageEl, 'Deny', approvalData, actions, confidenceSection);

    actions.appendChild(approveBtn);
    actions.appendChild(denyBtn);
    card.appendChild(actions);

    contentDiv.appendChild(card);
}

function renderConfidenceControls(card) {
    const section = document.createElement('div');
    section.className = 'confidence-section';

    const defaultH = 190;
    const defaultS = 75;
    const defaultL = 60;

    section.dataset.h = defaultH;
    section.dataset.s = defaultS;
    section.dataset.l = defaultL;

    const updatePreview = () => {
        const h = parseInt(section.dataset.h);
        const s = parseInt(section.dataset.s);
        const l = parseInt(section.dataset.l);

        const hex = hslToHex(h, s, l);
        const interp = interpretConfidence(h, s, l);

        preview.style.backgroundColor = hex;
        preview.textContent = hex;

        interpretation.innerHTML = `
            Domain: <span>${interp.domain}</span><br>
            Conviction: <span>${interp.conviction}</span><br>
            Clarity: <span>${interp.clarity}</span>
        `;
    };

    const hueSlider = document.createElement('div');
    hueSlider.className = 'confidence-slider';
    hueSlider.innerHTML = `
        <div class="confidence-slider-label">
            <span>Domain (Hue)</span>
            <span class="value" id="hue-value">${defaultH}°</span>
        </div>
        <input type="range" min="0" max="360" value="${defaultH}" id="hue-input">
    `;
    const hueInput = hueSlider.querySelector('input');
    const hueValue = hueSlider.querySelector('.value');
    hueInput.oninput = () => {
        section.dataset.h = hueInput.value;
        hueValue.textContent = `${hueInput.value}°`;
        updatePreview();
    };

    const satSlider = document.createElement('div');
    satSlider.className = 'confidence-slider';
    satSlider.innerHTML = `
        <div class="confidence-slider-label">
            <span>Conviction (Saturation)</span>
            <span class="value" id="sat-value">${defaultS}%</span>
        </div>
        <input type="range" min="0" max="100" value="${defaultS}" id="sat-input">
    `;
    const satInput = satSlider.querySelector('input');
    const satValue = satSlider.querySelector('.value');
    satInput.oninput = () => {
        section.dataset.s = satInput.value;
        satValue.textContent = `${satInput.value}%`;
        updatePreview();
    };

    const lightSlider = document.createElement('div');
    lightSlider.className = 'confidence-slider';
    lightSlider.innerHTML = `
        <div class="confidence-slider-label">
            <span>Clarity (Lightness)</span>
            <span class="value" id="light-value">${defaultL}%</span>
        </div>
        <input type="range" min="0" max="100" value="${defaultL}" id="light-input">
    `;
    const lightInput = lightSlider.querySelector('input');
    const lightValue = lightSlider.querySelector('.value');
    lightInput.oninput = () => {
        section.dataset.l = lightInput.value;
        lightValue.textContent = `${lightInput.value}%`;
        updatePreview();
    };

    const preview = document.createElement('div');
    preview.className = 'color-preview';

    const interpretation = document.createElement('div');
    interpretation.className = 'confidence-interpretation';

    section.appendChild(hueSlider);
    section.appendChild(satSlider);
    section.appendChild(lightSlider);
    section.appendChild(preview);
    section.appendChild(interpretation);

    updatePreview();

    return section;
}

function renderYesNoCard(contentDiv, questionText, messageEl) {
    const card = document.createElement('div');
    card.className = 'card yes-no-card';

    const header = document.createElement('div');
    header.className = 'card__header';

    const icon = document.createElement('span');
    icon.className = 'card__icon';
    icon.textContent = '❓';
    header.appendChild(icon);

    const title = document.createElement('h3');
    title.className = 'card__title';
    title.textContent = 'Confirmation Request';
    header.appendChild(title);

    card.appendChild(header);

    const body = document.createElement('div');
    body.className = 'card__body';

    const question = document.createElement('p');
    question.className = 'card__description';
    question.textContent = questionText;
    body.appendChild(question);

    card.appendChild(body);

    const actions = document.createElement('div');
    actions.className = 'card__actions';

    const yesButton = document.createElement('button');
    yesButton.className = 'btn btn--success';
    yesButton.textContent = 'Yes';
    yesButton.onclick = () => handleYesNoClick(messageEl, 'Yes', card, actions);

    const noButton = document.createElement('button');
    noButton.className = 'btn btn--error';
    noButton.textContent = 'No';
    noButton.onclick = () => handleYesNoClick(messageEl, 'No', card, actions);

    actions.appendChild(yesButton);
    actions.appendChild(noButton);
    card.appendChild(actions);

    contentDiv.appendChild(card);
}

// ============================================
// Input Card Rendering
// ============================================

function renderInputCard(contentDiv, inputData, message) {
    const type = inputData.type;

    if (type === 'text') {
        renderTextInput(contentDiv, inputData, message);
    } else if (type === 'slider') {
        if (inputData.scale && inputData.scale.low && inputData.scale.high) {
            renderVRGBSlider(contentDiv, inputData, message);
        } else {
            renderSliderInput(contentDiv, inputData, message);
        }
    } else if (type === 'choice') {
        renderChoiceInput(contentDiv, inputData, message);
    } else {
        console.error('Unknown input type:', type);
    }
}

function renderTextInput(contentDiv, inputData, message) {
    const card = document.createElement('div');
    card.className = 'card input-card';

    const header = document.createElement('div');
    header.className = 'card__header';
    const icon = document.createElement('span');
    icon.className = 'card__icon';
    icon.textContent = '✏️';
    const title = document.createElement('h3');
    title.className = 'card__title';
    title.textContent = 'Text Input';
    header.appendChild(icon);
    header.appendChild(title);
    card.appendChild(header);

    const body = document.createElement('div');
    body.className = 'card__body';

    const question = document.createElement('p');
    question.className = 'card__description';
    question.textContent = inputData.question;
    body.appendChild(question);

    const key = inputData.key || 'input';
    if (inputData.key) {
        const keyLabel = document.createElement('div');
        keyLabel.className = 'input-key-label';
        keyLabel.textContent = `Key: ${key}`;
        body.appendChild(keyLabel);
    }

    const textarea = document.createElement('textarea');
    textarea.className = 'input-text-area';
    textarea.placeholder = `Enter ${key}...`;
    body.appendChild(textarea);

    card.appendChild(body);

    const actions = document.createElement('div');
    actions.className = 'card__actions';

    const submitBtn = document.createElement('button');
    submitBtn.className = 'btn btn--primary';
    submitBtn.textContent = 'Submit';
    submitBtn.onclick = () => {
        const kvPair = {
            key: key,
            value: textarea.value
        };
        handleInputSubmit(message, kvPair, card, submitBtn);
    };
    actions.appendChild(submitBtn);

    card.appendChild(actions);
    contentDiv.appendChild(card);
}

function renderSliderInput(contentDiv, inputData, message) {
    const card = document.createElement('div');
    card.className = 'card input-card';

    const header = document.createElement('div');
    header.className = 'card__header';
    const icon = document.createElement('span');
    icon.className = 'card__icon';
    icon.textContent = '🎨';
    const title = document.createElement('span');
    title.className = 'input-title';
    title.textContent = 'HSL Slider Input';
    header.appendChild(icon);
    header.appendChild(title);
    card.appendChild(header);

    const question = document.createElement('div');
    question.className = 'input-question';
    question.textContent = inputData.question;
    card.appendChild(question);

    const sliderSection = renderConfidenceControls(card);
    sliderSection.classList.remove('collapsed');
    card.appendChild(sliderSection);

    const submitBtn = document.createElement('button');
    submitBtn.className = 'btn btn--primary input-submit-button';
    submitBtn.textContent = 'Submit';
    submitBtn.onclick = () => {
        const h = parseInt(sliderSection.dataset.h);
        const s = parseInt(sliderSection.dataset.s);
        const l = parseInt(sliderSection.dataset.l);
        const hex = hslToHex(h, s, l);
        const interp = interpretConfidence(h, s, l);
        const inputValue = {
            hsl: { h, s, l },
            hex: hex,
            interpretation: interp
        };
        if (inputData.key) {
            inputValue.key = inputData.key;
        }
        handleInputSubmit(message, inputValue, card, submitBtn);
    };
    card.appendChild(submitBtn);

    contentDiv.appendChild(card);
}

function renderVRGBSlider(contentDiv, inputData, message) {
    const card = document.createElement('div');
    card.className = 'card input-card';

    const header = document.createElement('div');
    header.className = 'card__header';
    const icon = document.createElement('span');
    icon.className = 'card__icon';
    icon.textContent = '📊';
    const title = document.createElement('span');
    title.className = 'input-title';
    title.textContent = 'Parameter Input';
    header.appendChild(icon);
    header.appendChild(title);
    card.appendChild(header);

    const question = document.createElement('div');
    question.className = 'input-question';
    question.textContent = inputData.question;
    card.appendChild(question);

    const sliderContainer = document.createElement('div');
    sliderContainer.style.padding = '20px';

    const scaleLabels = document.createElement('div');
    scaleLabels.style.display = 'flex';
    scaleLabels.style.justifyContent = 'space-between';
    scaleLabels.style.marginBottom = '10px';
    scaleLabels.style.fontSize = '14px';
    scaleLabels.style.color = '#666';

    const lowLabel = document.createElement('span');
    lowLabel.textContent = inputData.scale?.low || 'low';
    const highLabel = document.createElement('span');
    highLabel.textContent = inputData.scale?.high || 'high';
    scaleLabels.appendChild(lowLabel);
    scaleLabels.appendChild(highLabel);
    sliderContainer.appendChild(scaleLabels);

    const slider = document.createElement('input');
    slider.type = 'range';
    slider.min = '0';
    slider.max = '100';
    slider.value = '50';
    slider.style.width = '100%';

    const valueDisplay = document.createElement('div');
    valueDisplay.style.textAlign = 'center';
    valueDisplay.style.marginTop = '10px';
    valueDisplay.style.fontSize = '18px';
    valueDisplay.style.fontWeight = 'bold';
    valueDisplay.textContent = '50';

    slider.oninput = () => {
        valueDisplay.textContent = slider.value;
    };

    sliderContainer.appendChild(slider);
    sliderContainer.appendChild(valueDisplay);
    card.appendChild(sliderContainer);

    const submitBtn = document.createElement('button');
    submitBtn.className = 'btn btn--primary input-submit-button';
    submitBtn.textContent = 'Submit';
    submitBtn.onclick = () => {
        const sliderValue = parseInt(slider.value);

        const h = Math.round(sliderValue * 3.6);
        const s = 50;
        const l = 50;

        const hex = hslToHex(h, s, l);

        const interpretation = {
            domain: inputData.scale?.low || 'low',
            conviction: inputData.scale?.high || 'high',
            clarity: `${sliderValue}/100`
        };

        const semanticLabel = inputData.semantic_label ||
                             (inputData.question ? inputData.question.toLowerCase().replace(/[^a-z0-9]/g, '_') : 'param');

        const inputValue = {
            hsl: { h, s, l },
            hex: hex,
            interpretation: interpretation,
            slider_value: sliderValue,
            semantic_label: semanticLabel,
            question: inputData.question,
            scale: inputData.scale
        };

        handleInputSubmit(message, inputValue, card, submitBtn);
    };
    card.appendChild(submitBtn);

    contentDiv.appendChild(card);
}

function renderChoiceInput(contentDiv, inputData, message) {
    const card = document.createElement('div');
    card.className = 'card input-card';

    const header = document.createElement('div');
    header.className = 'card__header';
    const icon = document.createElement('span');
    icon.className = 'card__icon';
    icon.textContent = '☑️';
    const title = document.createElement('span');
    title.className = 'input-title';
    title.textContent = 'Multiple Choice';
    header.appendChild(icon);
    header.appendChild(title);
    card.appendChild(header);

    const question = document.createElement('div');
    question.className = 'input-question';
    question.textContent = inputData.question;
    card.appendChild(question);

    const optionsDiv = document.createElement('div');
    optionsDiv.className = 'choice-options';

    inputData.options.forEach((option, index) => {
        const optionBtn = document.createElement('div');
        optionBtn.className = 'choice-option';

        if (option.hsl) {
            const colorIndicator = document.createElement('div');
            colorIndicator.className = 'choice-color-indicator';
            const hex = hslToHex(option.hsl.h, option.hsl.s, option.hsl.l);
            colorIndicator.style.backgroundColor = hex;
            optionBtn.appendChild(colorIndicator);
        }

        const optionText = document.createElement('span');
        optionText.textContent = option.label;
        optionBtn.appendChild(optionText);

        optionBtn.onclick = () => handleChoiceClick(message, option, card, optionsDiv);

        optionsDiv.appendChild(optionBtn);
    });

    card.appendChild(optionsDiv);
    contentDiv.appendChild(card);
}

// ============================================
// Event Handlers
// ============================================

function handleInputSubmit(messageEl, inputValue, card, submitBtn) {
    submitBtn.disabled = true;

    const responseDiv = document.createElement('div');
    responseDiv.className = 'input-response-display';
    responseDiv.textContent = `Submitted: ${typeof inputValue === 'string' ? inputValue : JSON.stringify(inputValue)}`;
    card.appendChild(responseDiv);

    approvalPending = false;
    updateStatusText('idle');
    updateSendButtonState();

    const userMessage = typeof inputValue === 'string' ? inputValue : JSON.stringify(inputValue);
    addMessage('user', userMessage);

    socket.emit('input_response', { input: inputValue });
}

function handleChoiceClick(messageEl, selectedOption, card, optionsDiv) {
    const options = optionsDiv.querySelectorAll('.choice-option');
    options.forEach(opt => opt.style.pointerEvents = 'none');

    options.forEach(opt => opt.style.opacity = '0.5');
    event.target.closest('.choice-option').style.opacity = '1';
    event.target.closest('.choice-option').style.borderColor = '#1a73e8';

    const responseDiv = document.createElement('div');
    responseDiv.className = 'input-response-display';
    responseDiv.textContent = `Selected: ${selectedOption.label}`;
    card.appendChild(responseDiv);

    approvalPending = false;
    updateStatusText('idle');
    updateSendButtonState();

    addMessage('user', selectedOption.label);

    socket.emit('input_response', { choice: selectedOption });
}

function handleApprovalClick(messageEl, decision, approvalData, buttonsDiv, confidenceSection) {
    const buttons = buttonsDiv.querySelectorAll('.approval-button');
    buttons.forEach(btn => btn.disabled = true);

    const decisionDiv = document.createElement('div');
    decisionDiv.className = `approval-decision ${decision.toLowerCase()}d`;
    const timestamp = Date.now();
    decisionDiv.dataset.timestamp = timestamp;
    decisionDiv.textContent = `${decision}d (${getRelativeTime(timestamp)})`;
    messageEl.querySelector('.approval-card').appendChild(decisionDiv);

    approvalPending = false;
    updateStatusText('idle');
    updateSendButtonState();

    addMessage('user', `${decision} (${approvalData.action} on ${approvalData.target})`);

    const confidence = {
        h: parseInt(confidenceSection.dataset.h),
        s: parseInt(confidenceSection.dataset.s),
        l: parseInt(confidenceSection.dataset.l)
    };

    socket.emit('approval_response', {
        decision: decision,
        approval_data: approvalData,
        confidence: confidence
    });
}

function handleYesNoClick(messageEl, answer, card, buttonsDiv) {
    const buttons = buttonsDiv.querySelectorAll('.yes-no-button');
    buttons.forEach(btn => btn.disabled = true);

    const decisionDiv = document.createElement('div');
    decisionDiv.className = `yes-no-decision ${answer.toLowerCase()}`;
    const selectionTime = Date.now();
    decisionDiv.dataset.timestamp = selectionTime;
    decisionDiv.textContent = `Selected: ${answer} (${getRelativeTime(selectionTime)})`;
    card.appendChild(decisionDiv);

    yesNoQuestionPending = false;
    updateStatusText('idle');
    updateSendButtonState();

    addMessage('user', answer);

    socket.emit('button_response', { answer: answer });
}

// ============================================
// State Management
// ============================================

function setState(state) {
    currentState = state;
    dot.className = `dot ${state}`;
    miniStatusDot.className = `mini-status-dot ${state}`;
    miniStatusText.textContent = state;
    updateStatusText(state);

    if (state === 'speaking') {
        stopButton.classList.add('visible');
        miniStopLink.classList.add('visible');
    } else {
        stopButton.classList.remove('visible');
        miniStopLink.classList.remove('visible');
    }

    if (state === 'thinking' || state === 'idle') {
        if (!activityTimer) {
            activityStartTime = Date.now();
            estimatedTokens = 0;
            activityTimer = setInterval(() => {
                const elapsed = Math.floor((Date.now() - activityStartTime) / 1000);
                if (currentState === 'thinking') {
                    estimatedTokens += Math.floor(Math.random() * 50) + 25;
                    const tokenStr = estimatedTokens >= 1000 ? (estimatedTokens / 1000).toFixed(1) + 'k' : estimatedTokens;
                    updateStatusText('thinking', `(${elapsed}s) ${tokenStr} tokens`);
                } else if (currentState === 'idle') {
                    updateStatusText('idle', `(${elapsed}s)`);
                }
            }, 200);
        } else {
            const elapsed = Math.floor((Date.now() - activityStartTime) / 1000);
            if (state === 'thinking') {
                const tokenStr = estimatedTokens >= 1000 ? (estimatedTokens / 1000).toFixed(1) + 'k' : estimatedTokens;
                updateStatusText('thinking', `(${elapsed}s) ${tokenStr} tokens`);
            } else {
                updateStatusText('idle', `(${elapsed}s)`);
            }
        }
    } else {
        if (activityTimer) {
            clearInterval(activityTimer);
            activityTimer = null;
            estimatedTokens = 0;
        }
    }
}

// ============================================
// Timestamp Updates
// ============================================

function updateAllTimestamps() {
    const messages = document.querySelectorAll('.message[data-timestamp]');
    messages.forEach(message => {
        const timestamp = parseInt(message.dataset.timestamp);
        const timestampDiv = message.querySelector('.message-timestamp');
        if (timestampDiv) {
            timestampDiv.textContent = getRelativeTime(timestamp);
        }
    });

    const selections = document.querySelectorAll('.selected-answer[data-timestamp]');
    selections.forEach(selection => {
        const timestamp = parseInt(selection.dataset.timestamp);
        const text = selection.textContent;
        const match = text.match(/Selected: (Yes|No)/);
        if (match) {
            selection.textContent = `Selected: ${match[1]} (${getRelativeTime(timestamp)})`;
        }
    });
}

setInterval(updateAllTimestamps, 30000);

// ============================================
// Initialize
// ============================================

initAudio();

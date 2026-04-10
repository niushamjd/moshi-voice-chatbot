let ws = null;
let audioContext = null;
let processor = null;
let connected = false;

function toggleConnect() {
  if (connected) {
    disconnect();
  } else {
    connectToMoshi();
  }
}

function connectToMoshi() {
  ws = new WebSocket('ws://localhost:8998/api/chat');
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    connected = true;
    setStatus('active', 'Listening...', 'Disconnect');
    startMic();
  };

  ws.onmessage = (e) => {
    setRing('speaking');
    playAudio(e.data);
    setTimeout(() => setRing('active'), 800);
  };

  ws.onclose = () => disconnect();
  ws.onerror = () => {
    addMessage('moshi', 'Connection error — is the server running on port 8998?');
    disconnect();
  };
}

function disconnect() {
  connected = false;
  if (processor) processor.disconnect();
  if (audioContext) audioContext.close();
  if (ws) ws.close();
  ws = null;
  setStatus('', 'Click to connect', 'Connect');
}

function startMic() {
  navigator.mediaDevices.getUserMedia({ audio: true, video: false })
    .then(stream => {
      audioContext = new AudioContext({ sampleRate: 24000 });
      const source = audioContext.createMediaStreamSource(stream);
      processor = audioContext.createScriptProcessor(1920, 1, 1);

      processor.onaudioprocess = (e) => {
        if (ws && ws.readyState === WebSocket.OPEN) {
          const float32 = e.inputBuffer.getChannelData(0);
          const int16 = new Int16Array(float32.length);
          for (let i = 0; i < float32.length; i++) {
            int16[i] = Math.max(-32768, Math.min(32767, float32[i] * 32768));
          }
          ws.send(int16.buffer);
        }
      };

      source.connect(processor);
      processor.connect(audioContext.destination);
    })
    .catch(() => {
      addMessage('moshi', 'Microphone access denied. Please allow mic in browser settings.');
      disconnect();
    });
}

function playAudio(arrayBuffer) {
  const ctx = new AudioContext({ sampleRate: 24000 });
  ctx.decodeAudioData(arrayBuffer.slice(0), (buffer) => {
    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(ctx.destination);
    src.start();
  });
}

function setStatus(ringClass, statusText, btnText) {
  const ring = document.getElementById('ring');
  ring.className = 'status-ring ' + ringClass;
  document.getElementById('statusText').textContent = statusText;
  const btn = document.getElementById('connectBtn');
  btn.textContent = btnText;
  btn.className = ringClass === 'active' ? 'connected' : '';
}

function setRing(cls) {
  document.getElementById('ring').className = 'status-ring ' + cls;
}

function addMessage(role, text) {
  const t = document.getElementById('transcript');
  const placeholder = t.querySelector('.placeholder');
  if (placeholder) placeholder.remove();
  const p = document.createElement('p');
  p.className = role === 'user' ? 'msg-user' : 'msg-moshi';
  p.textContent = (role === 'user' ? 'You: ' : 'Moshi: ') + text;
  t.appendChild(p);
  t.scrollTop = t.scrollHeight;
}
// Browser client for the Moshi WebSocket protocol (see backend/audio_utils.py).
// Works with this repo's backend and with Kyutai's `moshi_mlx.local_web`.
//
// mic -> capture worklet (24 kHz PCM) -> WebCodecs Opus encoder -> Ogg pages -> WebSocket
// WebSocket -> Ogg pages -> WebCodecs Opus decoder -> player worklet -> speakers

import { OggOpusReader, OggOpusWriter } from './ogg.js';

const MODEL_RATE = 24000;
const KIND = { HANDSHAKE: 0, AUDIO: 1, TEXT: 2, ERROR: 5 };
// Start a new transcript line when Moshi has been quiet this long.
const TURN_GAP_MS = 1500;
const SPEAKING_RMS = 0.02;

let call = null; // state of the active connection, null when idle

function serverUrl() {
  const override = new URLSearchParams(location.search).get('server');
  if (override) return override;
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${scheme}://${location.host}/api/chat`;
}

function unsupportedReason() {
  if (location.protocol === 'file:') return 'Open this page through the server (http://localhost:8998), not as a file.';
  if (!window.AudioWorkletNode) return 'This browser does not support AudioWorklet.';
  if (!window.AudioEncoder || !window.AudioDecoder) return 'This browser does not support WebCodecs audio (use a recent Chrome, Edge, Firefox or Safari).';
  return null;
}

async function createAudioContext() {
  // Prefer running the graph at Moshi's rate so the browser does the resampling.
  try {
    return new AudioContext({ sampleRate: MODEL_RATE, latencyHint: 'interactive' });
  } catch {
    return new AudioContext({ latencyHint: 'interactive' });
  }
}

async function connect() {
  const reason = unsupportedReason();
  if (reason) return addNotice(reason);

  setUi('connecting', 'Connecting...', 'Cancel');
  const encoderConfig = { codec: 'opus', sampleRate: MODEL_RATE, numberOfChannels: 1, bitrate: 32000, opus: { frameDuration: 20000 } };
  if (!(await AudioEncoder.isConfigSupported(encoderConfig)).supported) {
    setUi('', 'Click to connect', 'Connect');
    return addNotice('This browser cannot encode Opus at 24 kHz.');
  }

  const c = (call = { ws: null, ctx: null, stream: null, encoder: null, decoder: null, lastTextAt: 0, line: null });
  try {
    c.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      video: false,
    });
  } catch {
    disconnect();
    return addNotice('Microphone access denied. Please allow the mic in your browser settings.');
  }
  if (call !== c) return; // cancelled while the permission prompt was open

  c.ctx = await createAudioContext();
  try {
    await c.ctx.audioWorklet.addModule('audio-worklets.js');
  } catch {
    disconnect();
    return addNotice('Could not load the audio worklets.');
  }
  if (call !== c) return;

  c.player = new AudioWorkletNode(c.ctx, 'player-processor');
  c.player.connect(c.ctx.destination);
  c.player.port.onmessage = (e) => onPlayerStats(e.data);

  c.ws = new WebSocket(serverUrl());
  c.ws.binaryType = 'arraybuffer';
  c.ws.onmessage = (e) => onServerMessage(c, new Uint8Array(e.data));
  c.ws.onerror = () => {
    if (call === c) addNotice(`Connection error. Is the server running at ${serverUrl()}?`);
  };
  c.ws.onclose = () => {
    if (call === c) disconnect();
  };

  setupDecoder(c);
  setupEncoder(c, encoderConfig);
}

function setupEncoder(c, config) {
  const ogg = new OggOpusWriter(MODEL_RATE);
  let headersSent = false;
  c.encoder = new AudioEncoder({
    output: (chunk) => {
      if (c.ws.readyState !== WebSocket.OPEN) return;
      if (!headersSent) {
        send(c.ws, KIND.AUDIO, ogg.headerPages());
        headersSent = true;
      }
      const packet = new Uint8Array(chunk.byteLength);
      chunk.copyTo(packet);
      send(c.ws, KIND.AUDIO, ogg.audioPage(packet, chunk.duration ?? 20000));
    },
    error: (e) => {
      addNotice(`Audio encoder error: ${e.message}`);
      disconnect();
    },
  });
  c.encoder.configure(config);
}

function setupDecoder(c) {
  let timestamp = 0;
  c.decoder = new AudioDecoder({
    output: (audio) => {
      const samples = new Float32Array(audio.numberOfFrames);
      audio.copyTo(samples, { planeIndex: 0, format: 'f32-planar' });
      c.player.port.postMessage({ samples, rate: audio.sampleRate }, [samples.buffer]);
      audio.close();
    },
    error: (e) => {
      addNotice(`Audio decoder error: ${e.message}`);
      disconnect();
    },
  });
  c.oggReader = new OggOpusReader({
    onHead: (head) => {
      c.decoder.configure({ codec: 'opus', sampleRate: MODEL_RATE, numberOfChannels: 1, description: head });
    },
    onPacket: (packet) => {
      if (c.decoder.state !== 'configured') return;
      c.decoder.decode(new EncodedAudioChunk({ type: 'key', timestamp, data: packet }));
      timestamp += 20000;
    },
  });
}

function startMicrophone(c) {
  const source = c.ctx.createMediaStreamSource(c.stream);
  const capture = new AudioWorkletNode(c.ctx, 'capture-processor');
  let timestamp = 0;
  capture.port.onmessage = (e) => {
    if (c.encoder.state !== 'configured') return;
    const data = e.data;
    const audio = new AudioData({
      format: 'f32-planar',
      sampleRate: MODEL_RATE,
      numberOfChannels: 1,
      numberOfFrames: data.length,
      timestamp,
      data,
    });
    timestamp += (data.length / MODEL_RATE) * 1e6;
    c.encoder.encode(audio);
    audio.close();
  };
  // The capture node outputs silence; connecting it to the destination keeps
  // the graph pulling it in every browser.
  source.connect(capture).connect(c.ctx.destination);
}

function onServerMessage(c, message) {
  if (call !== c || message.length === 0) return;
  const kind = message[0];
  const payload = message.subarray(1);
  switch (kind) {
    case KIND.HANDSHAKE:
      setUi('active', 'Listening. Just start talking.', 'Disconnect');
      startMicrophone(c);
      break;
    case KIND.AUDIO:
      try {
        c.oggReader.push(payload);
      } catch (e) {
        addNotice(`Bad audio stream from server: ${e.message}`);
        disconnect();
      }
      break;
    case KIND.TEXT:
      appendMoshiText(c, new TextDecoder().decode(payload));
      break;
    case KIND.ERROR:
      addNotice(`Server: ${new TextDecoder().decode(payload)}`);
      break;
  }
}

function send(ws, kind, payload) {
  const message = new Uint8Array(payload.length + 1);
  message[0] = kind;
  message.set(payload, 1);
  ws.send(message);
}

function disconnect() {
  const c = call;
  call = null;
  if (c) {
    if (c.ws) c.ws.close();
    if (c.stream) c.stream.getTracks().forEach((t) => t.stop());
    for (const codec of [c.encoder, c.decoder]) {
      if (codec && codec.state !== 'closed') codec.close();
    }
    if (c.ctx) c.ctx.close();
  }
  setUi('', 'Click to connect', 'Connect');
  document.getElementById('stats').textContent = '';
}

function onPlayerStats({ rms, bufferedMs, underruns }) {
  if (!call || !call.ws || call.ws.readyState !== WebSocket.OPEN) return;
  setRing(rms > SPEAKING_RMS ? 'speaking' : 'active');
  document.getElementById('stats').textContent =
    `playback buffer ${bufferedMs.toFixed(0)} ms · underruns ${underruns}`;
}

function appendMoshiText(c, text) {
  const now = performance.now();
  if (!c.line || now - c.lastTextAt > TURN_GAP_MS) {
    c.line = addLine('msg-moshi', 'Moshi: ');
    text = text.trimStart();
  }
  c.lastTextAt = now;
  c.line.textContent += text;
  scrollTranscript();
}

function addNotice(text) {
  addLine('msg-notice', text);
  scrollTranscript();
}

function addLine(className, text) {
  const t = document.getElementById('transcript');
  t.querySelector('.placeholder')?.remove();
  const p = document.createElement('p');
  p.className = className;
  p.textContent = text;
  t.appendChild(p);
  return p;
}

function scrollTranscript() {
  const t = document.getElementById('transcript');
  t.scrollTop = t.scrollHeight;
}

function setUi(ringClass, statusText, buttonText) {
  setRing(ringClass);
  document.getElementById('statusText').textContent = statusText;
  const btn = document.getElementById('connectBtn');
  btn.textContent = buttonText;
  btn.className = ringClass === 'active' ? 'connected' : '';
}

function setRing(cls) {
  document.getElementById('ring').className = 'status-ring ' + cls;
}

document.getElementById('connectBtn').addEventListener('click', () => (call ? disconnect() : connect()));

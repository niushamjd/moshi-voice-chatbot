// AudioWorklet processors: microphone capture and gapless playback.
// Both run on the audio rendering thread, off the main thread.

const MODEL_RATE = 24000;
const CAPTURE_CHUNK = 480; // 20 ms at 24 kHz, one Opus packet

// Streaming linear resampler. Only used when the browser could not open the
// AudioContext at 24 kHz itself (which gives better quality resampling).
class LinearResampler {
  constructor(fromRate, toRate) {
    this.step = fromRate / toRate;
    this.pos = 0; // position of the next output sample; -1 refers to `prev`
    this.prev = 0;
  }

  process(input) {
    if (this.step === 1) return input;
    const out = [];
    const n = input.length;
    while (this.pos <= n - 1) {
      const i = Math.floor(this.pos);
      const frac = this.pos - i;
      const a = i < 0 ? this.prev : input[i];
      const b = i + 1 < n ? input[i + 1] : a;
      out.push(a + (b - a) * frac);
      this.pos += this.step;
    }
    this.pos -= n;
    if (n > 0) this.prev = input[n - 1];
    return Float32Array.from(out);
  }
}

class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.resampler = new LinearResampler(sampleRate, MODEL_RATE);
    this.chunk = new Float32Array(CAPTURE_CHUNK);
    this.filled = 0;
  }

  process(inputs) {
    const channel = inputs[0][0];
    if (!channel) return true;
    const samples = this.resampler.process(channel);
    for (let i = 0; i < samples.length; i++) {
      this.chunk[this.filled++] = samples[i];
      if (this.filled === CAPTURE_CHUNK) {
        this.port.postMessage(this.chunk, [this.chunk.buffer]);
        this.chunk = new Float32Array(CAPTURE_CHUNK);
        this.filled = 0;
      }
    }
    return true;
  }
}

// Plays decoded audio from a small jitter buffer. Moshi delivers audio in
// 80 ms bursts, so playback starts once PREBUFFER is queued. Latency is kept
// low two ways: a hard cap (MAX_BUFFER) for large bursts, e.g. after a server
// stall, and a trim when the buffer never drains below TRIM_ABOVE for a whole
// TRIM_WINDOW, meaning it holds latency it does not need for jitter.
const PREBUFFER_S = 0.1;
const MAX_BUFFER_S = 0.4;
const TARGET_AFTER_DROP_S = 0.12;
const TRIM_WINDOW_S = 2;
const TRIM_ABOVE_S = 0.08;
const TRIM_TO_S = 0.04;
const REPORT_INTERVAL_S = 0.1;

class PlayerProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.offset = 0; // read position inside queue[0]
    this.buffered = 0;
    this.playing = false;
    this.resampler = null;
    this.inputRate = 0;
    this.underruns = 0;
    this.windowMin = Infinity;
    this.windowFrames = 0;
    this.sumSquares = 0;
    this.reportFrames = 0;
    this.port.onmessage = (e) => this.receive(e.data);
  }

  receive({ samples, rate }) {
    if (rate !== this.inputRate) {
      this.inputRate = rate;
      this.resampler = new LinearResampler(rate, sampleRate);
    }
    const resampled = this.resampler.process(samples);
    this.queue.push(resampled);
    this.buffered += resampled.length;
    if (this.buffered > MAX_BUFFER_S * sampleRate) {
      this.skip(this.buffered - TARGET_AFTER_DROP_S * sampleRate);
    }
  }

  skip(count) {
    while (count > 0 && this.queue.length) {
      const available = this.queue[0].length - this.offset;
      const n = Math.min(available, count);
      this.offset += n;
      this.buffered -= n;
      count -= n;
      if (this.offset === this.queue[0].length) {
        this.queue.shift();
        this.offset = 0;
      }
    }
  }

  trimStandingLatency(frames) {
    if (!this.playing) {
      this.windowMin = Infinity;
      this.windowFrames = 0;
      return;
    }
    this.windowMin = Math.min(this.windowMin, this.buffered);
    this.windowFrames += frames;
    if (this.windowFrames >= TRIM_WINDOW_S * sampleRate) {
      if (this.windowMin > TRIM_ABOVE_S * sampleRate) this.skip(this.windowMin - TRIM_TO_S * sampleRate);
      this.windowMin = Infinity;
      this.windowFrames = 0;
    }
  }

  process(_, outputs) {
    const out = outputs[0][0];
    if (!this.playing && this.buffered >= PREBUFFER_S * sampleRate) this.playing = true;

    let written = 0;
    if (this.playing) {
      while (written < out.length && this.queue.length) {
        const head = this.queue[0];
        const n = Math.min(out.length - written, head.length - this.offset);
        out.set(head.subarray(this.offset, this.offset + n), written);
        written += n;
        this.offset += n;
        this.buffered -= n;
        if (this.offset === head.length) {
          this.queue.shift();
          this.offset = 0;
        }
      }
      if (written < out.length) {
        // Ran dry: go back to buffering so the next audio plays smoothly.
        this.playing = false;
        this.underruns++;
      }
    }
    out.fill(0, written);
    this.trimStandingLatency(out.length);

    for (let i = 0; i < written; i++) this.sumSquares += out[i] * out[i];
    this.reportFrames += out.length;
    if (this.reportFrames >= REPORT_INTERVAL_S * sampleRate) {
      this.port.postMessage({
        rms: Math.sqrt(this.sumSquares / this.reportFrames),
        bufferedMs: (this.buffered / sampleRate) * 1000,
        underruns: this.underruns,
      });
      this.sumSquares = 0;
      this.reportFrames = 0;
    }
    return true;
  }
}

registerProcessor('capture-processor', CaptureProcessor);
registerProcessor('player-processor', PlayerProcessor);

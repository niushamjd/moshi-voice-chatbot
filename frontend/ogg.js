// Minimal Ogg container support for a single mono Opus stream (RFC 3533, RFC 7845).
// WebCodecs produces and consumes raw Opus packets; the Moshi protocol carries
// them inside an Ogg stream, so we mux on the way out and demux on the way in.

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let r = i << 24;
    for (let j = 0; j < 8; j++) r = r & 0x80000000 ? (r << 1) ^ 0x04c11db7 : r << 1;
    table[i] = r >>> 0;
  }
  return table;
})();

function oggCrc(bytes) {
  let crc = 0;
  for (let i = 0; i < bytes.length; i++) {
    crc = ((crc << 8) ^ CRC_TABLE[((crc >>> 24) ^ bytes[i]) & 0xff]) >>> 0;
  }
  return crc;
}

const OPUS_HEAD = [0x4f, 0x70, 0x75, 0x73, 0x48, 0x65, 0x61, 0x64]; // "OpusHead"
const OPUS_TAGS = [0x4f, 0x70, 0x75, 0x73, 0x54, 0x61, 0x67, 0x73]; // "OpusTags"

function startsWith(bytes, magic) {
  return bytes.length >= magic.length && magic.every((b, i) => bytes[i] === b);
}

export function opusHead(inputSampleRate, preSkip = 0) {
  const head = new Uint8Array(19);
  const view = new DataView(head.buffer);
  head.set(OPUS_HEAD, 0);
  head[8] = 1; // version
  head[9] = 1; // channels
  view.setUint16(10, preSkip, true);
  view.setUint32(12, inputSampleRate, true);
  view.setInt16(16, 0, true); // output gain
  head[18] = 0; // channel mapping family
  return head;
}

function opusTags() {
  const vendor = new TextEncoder().encode('moshi-voice-chatbot');
  const tags = new Uint8Array(8 + 4 + vendor.length + 4);
  const view = new DataView(tags.buffer);
  tags.set(OPUS_TAGS, 0);
  view.setUint32(8, vendor.length, true);
  tags.set(vendor, 12);
  view.setUint32(12 + vendor.length, 0, true); // no user comments
  return tags;
}

// Wraps Opus packets into Ogg pages, one packet per page for minimal latency.
export class OggOpusWriter {
  constructor(inputSampleRate) {
    this.serial = (Math.random() * 0xffffffff) >>> 0;
    this.sequence = 0;
    this.granule = 0; // always counted at 48 kHz for Opus
    this.inputSampleRate = inputSampleRate;
  }

  // The two mandatory header pages; send these before any audio.
  headerPages() {
    return concat([
      this.page(opusHead(this.inputSampleRate), 0x02, 0),
      this.page(opusTags(), 0x00, 0),
    ]);
  }

  // durationUs: the packet's duration in microseconds.
  audioPage(packet, durationUs) {
    this.granule += Math.round((durationUs * 48000) / 1e6);
    return this.page(packet, 0x00, this.granule);
  }

  page(packet, headerType, granule) {
    const segments = Math.floor(packet.length / 255) + 1;
    if (segments > 255) throw new Error('Opus packet too large for one Ogg page');
    const page = new Uint8Array(27 + segments + packet.length);
    const view = new DataView(page.buffer);
    page.set([0x4f, 0x67, 0x67, 0x53], 0); // "OggS"
    page[4] = 0; // version
    page[5] = headerType;
    view.setBigUint64(6, BigInt(granule), true);
    view.setUint32(14, this.serial, true);
    view.setUint32(18, this.sequence++, true);
    // CRC at offset 22 stays zero while it is computed.
    page[26] = segments;
    for (let i = 0; i < segments - 1; i++) page[27 + i] = 255;
    page[27 + segments - 1] = packet.length % 255;
    page.set(packet, 27 + segments);
    view.setUint32(22, oggCrc(page), true);
    return page;
  }
}

// Reassembles packets from a stream of Ogg bytes that may split pages and
// packets at arbitrary points. Calls onHead(OpusHead packet) once, then
// onPacket(Opus packet) for each audio packet.
export class OggOpusReader {
  constructor({ onHead, onPacket }) {
    this.onHead = onHead;
    this.onPacket = onPacket;
    this.buffer = new Uint8Array(0);
    this.partial = [];
  }

  push(bytes) {
    this.buffer = concat([this.buffer, bytes]);
    let offset = 0;
    while (this.buffer.length - offset >= 27) {
      const b = this.buffer;
      if (b[offset] !== 0x4f || b[offset + 1] !== 0x67 || b[offset + 2] !== 0x67 || b[offset + 3] !== 0x53) {
        throw new Error('lost Ogg sync');
      }
      const segments = b[offset + 26];
      const headerLength = 27 + segments;
      if (b.length - offset < headerLength) break;
      let bodyLength = 0;
      for (let i = 0; i < segments; i++) bodyLength += b[offset + 27 + i];
      if (b.length - offset < headerLength + bodyLength) break;

      let pos = offset + headerLength;
      for (let i = 0; i < segments; i++) {
        const lace = b[offset + 27 + i];
        this.partial.push(b.subarray(pos, pos + lace));
        pos += lace;
        if (lace < 255) this.emit(concat(this.partial));
      }
      offset += headerLength + bodyLength;
    }
    this.buffer = this.buffer.slice(offset);
  }

  emit(packet) {
    this.partial = [];
    if (startsWith(packet, OPUS_HEAD)) this.onHead(packet);
    else if (!startsWith(packet, OPUS_TAGS)) this.onPacket(packet);
  }
}

function concat(chunks) {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const c of chunks) {
    out.set(c, offset);
    offset += c.length;
  }
  return out;
}

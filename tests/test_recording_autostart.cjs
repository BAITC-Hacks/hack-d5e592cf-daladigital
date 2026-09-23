const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');

const source = readFileSync(new URL('../app/static/app.js', `file://${__filename}`), 'utf8')
  .split("$('#meeting-date').value=new Date()")[0];

function setup({mode = 'room', includeMic = false, noAudio = false, denied = false, serverError = false} = {}) {
  const elements = new Map();
  const element = selector => {
    if (!elements.has(selector)) elements.set(selector, {value: '', checked: false, hidden: false, style: {}});
    return elements.get(selector);
  };
  element('#meeting-title').value = 'Автоматическая запись';
  element('#meeting-date').value = '2026-09-23';
  element('#meeting-url').value = 'https://meet.google.com/abc-defg-hij';
  element('#consent').checked = true;
  element('#include-microphone').checked = includeMic;
  const tracks = [];
  const track = kind => {
    const value = {kind, readyState: 'live', stop() { this.readyState = 'ended'; }, addEventListener() {}};
    tracks.push(value);
    return value;
  };
  class Stream {
    constructor(tracks) { this.tracks = tracks; }
    getTracks() { return this.tracks; }
    getAudioTracks() { return this.tracks.filter(t => t.kind === 'audio'); }
  }
  class Recorder {
    static isTypeSupported() { return true; }
    constructor(stream) { this.stream = stream; this.state = 'inactive'; this.starts = 0; }
    addEventListener() {}
    start() { this.starts++; this.state = 'recording'; }
  }
  const intervals = new Set();
  const requests = [];
  const sandbox = vm.createContext({
    document: {querySelector: element}, window: {MediaRecorder: Recorder}, MediaRecorder: Recorder,
    MediaStream: Stream, URL, Blob, Date, AbortController, setTimeout, clearTimeout,
    setInterval(callback) { intervals.add(callback); return callback; },
    clearInterval(callback) { intervals.delete(callback); },
    navigator: {mediaDevices: {
      async getUserMedia() { if (denied) throw new Error('Permission denied'); return new Stream([track('audio')]); },
      async getDisplayMedia() { return new Stream([track('video'), ...(noAudio ? [] : [track('audio')])]); },
    }},
    AudioContext: class {
      state = 'running';
      async resume() {}
      async close() { this.state = 'closed'; }
      createMediaStreamSource() { return {connect(node) { return node; }}; }
      createGain() { return {gain: {}, connect() {}}; }
      createMediaStreamDestination() { return {stream: new Stream([track('audio')])}; }
      createAnalyser() { return {getFloatTimeDomainData(samples) { samples.fill(0); }}; }
    },
    async request(path, options) {
      requests.push({path, options});
      if (serverError) throw new Error('Server unavailable');
      return {id: 'meeting-test'};
    },
  });
  vm.runInContext(`${source}\napi = request; state.source = '${mode}'; state.notice = {version: 'v1'}; state.noticeAcknowledgedVersion = 'v1';`, sandbox);
  return {element, tracks, intervals, requests, run: code => vm.runInContext(code, sandbox)};
}

for (const options of [{mode: 'room'}, {mode: 'platform'}, {mode: 'platform', includeMic: true}]) {
  test(`starts automatically in silence: ${JSON.stringify(options)}`, async () => {
    const app = setup(options);
    await app.run('prepareCapture()');
    assert.equal(app.run('state.recorder.state'), 'recording');
    assert.equal(app.run('state.recorder.starts'), 1);
    assert.ok(app.run('state.startedAt') > 0);
    assert.equal(app.element('#recording-banner').hidden, false);
    assert.equal(app.element('#stop-recording').hidden, false);
    assert.equal(app.element('#cancel-capture').hidden, true);
    for (const tick of app.intervals) tick();
    assert.match(app.element('#record-status').textContent, /Нет сигнала/);
    assert.equal(app.run('state.recorder.state'), 'recording');
    await app.run('prepareCapture()');
    await app.run('beginRecording()');
    assert.equal(app.requests.length, 1, 'does not create duplicate recordings');
    await app.run("state.recorder.ondataavailable({data: new Blob(['audio'])}); state.uploadChain");
    assert.equal(app.requests[1].path, '/api/meetings/meeting-test/chunks');
    assert.equal(app.run('state.localChunks.length'), 1);
  });
}

for (const options of [{denied: true}, {mode: 'platform', noAudio: true}, {serverError: true}]) {
  test(`failed automatic start releases capture: ${JSON.stringify(options)}`, async () => {
    const app = setup(options);
    await assert.rejects(app.run('prepareCapture()'));
    assert.equal(app.run('state.recordingId'), null);
    assert.equal(app.run('state.stream'), null);
    assert.equal(app.run('state.starting'), false);
    assert.ok(app.tracks.every(track => track.readyState === 'ended'));
    assert.equal(app.intervals.size, 0);
    assert.equal(app.element('#meeting-form').hidden, false);
  });
}

test('automatic start still requires acknowledgement of the recording notice', async () => {
  const app = setup();
  app.element('#consent').checked = false;
  await assert.rejects(app.run('prepareCapture()'), /предупреждение/);
  assert.equal(app.requests.length, 0);
  assert.equal(app.run('state.stream'), null);
});

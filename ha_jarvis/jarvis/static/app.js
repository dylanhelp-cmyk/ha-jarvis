'use strict';

const $ = (id) => document.getElementById(id);
const labels = {latency: 'Playback latency', stop: 'Stop response', stability: 'Stream stability',
  wake_ack: 'Wake acknowledgment', echo: 'Quiet-room echo test', connection: 'Microphone connection'};
let kind = 'latency';
let state;
let busy = false;
let listenedClip;

async function api(path, method = 'GET', body) {
  const options = {method, cache: 'no-store'};
  if (method !== 'GET') {
    options.headers = {'Content-Type': 'application/json'};
    options.body = JSON.stringify(body ?? {});
  }
  const response = await fetch(path, options);
  const content = response.headers.get('Content-Type') || '';
  const payload = content.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok) throw new Error(payload.error || payload || 'Jarvis request failed');
  return payload;
}

function notice(message) {
  $('notice').hidden = false;
  $('notice').textContent = message;
}

function readiness() {
  const device = state?.device;
  const running = Boolean(state?.test.running);
  const speaker = kind !== 'connection';
  let reason = '';
  if (!device?.connected) reason = 'Connect the paired Voice PE.';
  else if (device.muted || !device.raw_ready) reason = 'Unmute Voice PE and wait for raw microphone verification.';
  else if (speaker && (device.playback_protocol !== 1 || !device.jack_plugged)) reason = 'Install compatible playback firmware and connect the 3.5 mm jack.';
  else if (speaker && (!$('volume-confirmed').checked || !$('idle-confirmed').checked)) reason = 'Confirm the speaker volume and idle state.';
  else if (kind === 'wake_ack' && !state?.acknowledgment.cached_on_device) reason = 'Verify and preload the wake acknowledgment first.';
  $('readiness').textContent = running ? state.test.phase : reason || 'Ready for a local test.';
  $('run-test').textContent = 'Run ' + labels[kind].toLowerCase();
  $('run-test').disabled = Boolean(reason || running || busy);
  $('cancel-test').hidden = !running;
  $('cancel-test').disabled = busy;
  $('save-config').disabled = running || busy;
  const ack = state?.acknowledgment;
  $('verify-ack').disabled = running || busy || !ack?.candidate_id || listenedClip !== ack.candidate_id;
  $('generate-ack').disabled = running || busy || !state?.cloud.api_key_configured || state.cloud.paid_retry_blocked;
}

function showStatus(data) {
  state = data;
  const device = data.device;
  $('connection-label').textContent = device.connected ? 'Voice PE connected' : 'Waiting for Voice PE';
  $('device-status').textContent = device.connected ? (device.muted ? 'Muted' : (device.raw_ready ? 'Ready' : 'Checking microphone')) : 'Disconnected';
  $('device-detail').textContent = device.firmware ? 'Firmware ' + device.firmware : 'Connect the bench firmware';
  $('route-status').textContent = 'Voice PE 3.5 mm';
  $('route-detail').textContent = !device.connected ? 'Waiting for device' : device.playback_protocol !== 1 ? 'Playback firmware needed' : device.jack_plugged ? 'Jack connected' : 'Jack unplugged';
  $('test-status').textContent = data.test.running ? 'Running' : 'Standby';
  $('test-detail').textContent = data.test.phase;
  $('version').textContent = 'App ' + data.version;
  $('cloud-state').textContent = (data.cloud.api_key_configured ? 'Key saved locally.' : 'No key saved.') + ' Paid playback is gated on wired verification.';
  const ack = data.acknowledgment;
  const preview = $('ack-preview');
  if (preview.dataset.clip !== (ack.candidate_id || '')) {
    preview.dataset.clip = ack.candidate_id || '';
    listenedClip = undefined;
    if (ack.candidate_id) preview.src = 'api/acknowledgment/preview?clip=' + encodeURIComponent(ack.candidate_id);
    else preview.removeAttribute('src');
  }
  preview.hidden = !ack.cached_on_host;
  $('verify-ack').hidden = !ack.cached_on_host || ack.verified;
  $('generate-ack').hidden = ack.verified;
  $('generate-ack').textContent = ack.cached_on_host ? 'Replace clip (paid API test)' : 'Generate clip (paid API test)';
  $('ack-state').textContent = ack.cached_on_device ? 'Verified clip cached on Voice PE.' :
    ack.verified ? 'Verified clip saved. Waiting for compatible Voice PE firmware to preload it.' :
    ack.cached_on_host ? 'Play the clip and confirm it says exactly “Yes sir”.' :
    ack.phase === 'failed' ? 'Generation failed. Check the API budget and key before retrying.' :
    'Generate the cached greeting in the selected GPT-Live voice.';
  readiness();
}

function add(parent, tag, value) {
  const element = document.createElement(tag);
  element.textContent = value;
  parent.append(element);
  return element;
}

function showResults(results) {
  if (!results.length) return;
  const list = $('results-list');
  list.replaceChildren();
  for (const result of results) {
    const card = document.createElement('article');
    card.className = 'result-card';
    list.append(card);
    add(card, 'h3', (labels[result.kind] || result.kind) + ' / ' + result.status);
    if (result.error) add(card, 'p', result.error);
    if (result.interruption) add(card, 'p', 'Interrupted by ' + result.interruption.reason.replaceAll('_', ' '));
    for (const trial of result.trials) {
      const cold = trial.cold_startup_ms;
      add(card, 'p', 'Trial ' + trial.trial + ' / ' +
        (trial.latency_ms == null ? (trial.reason || 'Acoustic measurement unavailable') : trial.latency_ms + ' ms') +
        (cold == null ? '' : ' / first sound ' + cold + ' ms after command'));
    }
    if (result.playback) add(card, 'p', result.playback.frames_accepted + ' frames accepted by the speaker API.');
    if (result.stability) add(card, 'p', result.stability.status === 'measured' ?
      'Pilot gaps ' + result.stability.dropout_count : result.stability.reason);
    if (result.echo) add(card, 'p', result.echo.status === 'measured' ?
      'Offline pilot suppression ' + result.echo.pilot_band_reduction_db + ' dB' : result.echo.reason);
    for (const warning of result.warnings || []) add(card, 'p', warning);
  }
}

async function poll() {
  showStatus(await api('api/status'));
  if (!state.test.running) showResults((await api('api/results')).results);
}

async function action(callback) {
  busy = true;
  readiness();
  try {
    await callback();
    await poll();
  } catch (error) {
    notice(error.message);
  } finally {
    busy = false;
    readiness();
  }
}

for (const button of document.querySelectorAll('.test-option')) {
  button.addEventListener('click', () => {
    kind = button.dataset.kind;
    for (const option of document.querySelectorAll('.test-option')) {
      option.classList.toggle('selected', option === button);
      option.setAttribute('aria-pressed', String(option === button));
    }
    readiness();
  });
}
for (const id of ['volume-confirmed', 'idle-confirmed']) $(id).addEventListener('change', readiness);

$('config-form').addEventListener('submit', (event) => {
  event.preventDefault();
  action(async () => {
    const config = {ha_lan_url: $('ha-lan-url').value.trim(), repetitions: Number($('repetitions').value)};
    if ($('device-token').value) config.device_token = $('device-token').value;
    await api('api/config', 'PUT', config);
    $('save-state').textContent = 'Connection saved.';
  });
});
$('reveal-token').addEventListener('click', () => {
  const input = $('device-token');
  const reveal = input.type === 'password';
  input.type = reveal ? 'text' : 'password';
  $('reveal-token').textContent = reveal ? 'Hide' : 'Show';
  $('reveal-token').setAttribute('aria-pressed', String(reveal));
});
$('copy-token').addEventListener('click', () => {
  navigator.clipboard.writeText($('device-token').value).catch(() => notice('Clipboard access unavailable.'));
});
$('run-test').addEventListener('click', () => action(() => api('api/tests', 'POST', {
  kind, comfortable_volume_confirmed: $('volume-confirmed').checked
})));
$('cancel-test').addEventListener('click', () => action(() => api('api/tests/current', 'DELETE')));
$('cloud-form').addEventListener('submit', (event) => {
  event.preventDefault();
  action(async () => {
    await api('api/cloud', 'PUT', {api_key: $('openai-key').value});
    $('openai-key').value = '';
  });
});

async function initialize() {
  const config = await api('api/config');
  $('ha-lan-url').value = config.ha_lan_url;
  $('repetitions').value = config.repetitions;
  $('device-token').value = config.device_token;
  $('save-state').textContent = 'Connection loaded.';
  await poll();
}

initialize().catch((error) => notice(error.message));
setInterval(() => { if (!busy) poll().catch((error) => notice(error.message)); }, 1500);

$('ack-preview').addEventListener('ended', (event) => {
  listenedClip = event.currentTarget.dataset.clip;
  readiness();
});
$('verify-ack').addEventListener('click', () => action(() => api('api/acknowledgment/verify', 'POST', {
  clip: listenedClip, heard_exact_phrase: true
})));
$('generate-ack').addEventListener('click', () => {
  if (window.confirm('Start one budgeted GPT-Live greeting test using digital silence?')) {
    action(() => api('api/cloud/acknowledgment', 'POST', {confirm_paid: true}));
  }
});

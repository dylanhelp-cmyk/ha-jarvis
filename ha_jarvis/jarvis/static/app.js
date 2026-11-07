'use strict';

const $ = (id) => document.getElementById(id);
const labels = {latency: 'Playback latency', stop: 'Stop response', stability: 'Stream stability',
  reply_stall: 'Speech recovery', synthetic_replies: 'Synthetic reply timing', wake_ack: 'Wake acknowledgment', render_alignment: 'Echo reference alignment', voice_turn: 'Voice preview', echo: 'Quiet-room echo test', connection: 'Microphone connection'};
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
  const wakeTest = kind === 'wake_ack';
  const startup = $('wake-startup-transport');
  $('wake-startup-options').hidden = !wakeTest;
  $('voice-turn-consent').hidden = kind !== 'voice_turn';
  startup.disabled = running || busy;
  const standby = kind === 'voice_turn' && $('voice-standby').checked;
  $('voice-preconnect').disabled = standby;
  $('voice-follow-up').disabled = standby;
  let reason = '';
  if (!device?.connected) reason = 'Connect the paired Voice PE.';
  else if (device.muted || !device.raw_ready) reason = 'Unmute Voice PE and wait for raw microphone verification.';
  else if (speaker && (device.playback_protocol !== 1 || !device.jack_plugged)) reason = 'Install compatible playback firmware and connect the 3.5 mm jack.';
  else if (speaker && (!$('volume-confirmed').checked || !$('idle-confirmed').checked)) reason = 'Confirm the speaker volume and idle state.';
  else if (kind === 'render_alignment' && !state?.acknowledgment.verified) reason = 'Verify the wake clip before testing render alignment.';
  else if (kind === 'wake_ack' && !state?.acknowledgment.cached_on_device) reason = 'Verify and preload the wake acknowledgment first.';
  else if (wakeTest && startup.value && (!state?.cloud.api_key_configured || state.cloud.paid_retry_blocked)) reason = 'A configured OpenAI key and a settled budget ledger are required.';
  else if (kind === 'voice_turn' && (!state.echo_alignment_ready || !state.acknowledgment.cached_on_device)) reason = 'Run a successful Echo reference alignment first.';
  else if (kind === 'voice_turn' && (!state.cloud.api_key_configured || state.cloud.paid_retry_blocked)) reason = 'A configured OpenAI key and a settled budget ledger are required.';
  else if (kind === 'voice_turn' && !$('voice-turn-confirmed').checked) reason = 'Review and authorize microphone audio for the selected voice mode.';
  $('readiness').textContent = running ? state.test.phase : reason || (kind === 'voice_turn' ? 'Ready for an authorized cloud voice preview.' : 'Ready for a local test.');
  $('run-test').textContent = standby ? 'Enable wake standby' : 'Run ' + labels[kind].toLowerCase();
  $('cancel-test').textContent = state?.test.standby ? 'Stop wake standby' : 'Stop test';
  $('run-test').disabled = Boolean(reason || running || busy);
  $('cancel-test').hidden = !running;
  $('cancel-test').disabled = busy;
  $('save-config').disabled = running || busy;
  const ack = state?.acknowledgment;
  $('verify-ack').disabled = running || busy || !ack?.candidate_id || listenedClip !== ack.candidate_id;
  $('run-synthetic-replies').disabled = running || busy || !device?.connected || device.muted ||
    !device.raw_ready || device.playback_protocol !== 1 || !device.jack_plugged ||
    !state?.cloud.api_key_configured || state.cloud.paid_retry_blocked ||
    !$('volume-confirmed').checked || !$('idle-confirmed').checked;
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
  const activeGain = data.test.input_gain_db;
  $('test-detail').textContent = data.test.phase + (activeGain == null ? '' :
    ' · ' + activeGain + ' dB App mic gain' +
    (device.capture_gain ? ' · ' + device.capture_gain + '× firmware mic gain' : ''));
  $('version').textContent = 'App ' + data.version;
  $('cloud-state').textContent = (data.cloud.api_key_configured ? 'Key saved locally.' : 'No key saved.') + ' Synthetic reply timing uses the local jack.';
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
    if (result.error || result.reason) add(card, 'p', result.error || result.reason);
    if (result.wake_capture_mode === 'device_armed') {
      const onset = result.wake_to_first_capture_sample_ms;
      add(card, 'p', onset == null ? 'Device-armed capture timing unavailable.' :
        'First microphone sample was timestamped ' + onset + ' ms relative to wake detection.');
      const opening = result.opening_audio;
      if (opening) add(card, 'p', opening.status === 'ready' ?
        'Opening buffer kept ' + opening.sample_count + ' samples in order.' :
        'Opening buffer requested a retry (' + opening.reason + ').');
    }
    if (result.cloud_readiness) {
      const ready = result.cloud_readiness;
      add(card, 'p', ready.wake_to_ready_ms == null ? 'GPT-Live readiness unavailable.' :
        ready.transport + ' GPT-Live ready ' + ready.wake_to_ready_ms + ' ms after wake detection.');
      if (ready.ready_minus_audible_ack_ms != null) {
        const lead = ready.ready_minus_audible_ack_ms;
        add(card, 'p', 'Audible acknowledgment ' + Math.abs(lead) + ' ms ' +
          (lead >= 0 ? 'before' : 'after') + ' cloud readiness.');
      }
    }
    if (result.interruption) add(card, 'p', 'Interrupted by ' + result.interruption.reason.replaceAll('_', ' '));
    if (result.kind === 'voice_turn' && result.digital_first_voice_ms != null) {
      add(card, 'p', 'First returned speech ' + result.digital_first_voice_ms + ' ms after input streaming began.');
    }
    if (result.input_signal) {
      const signal = result.input_signal;
      const peak = signal.peak_transmitted_pcm;
      const level = peak ? (20 * Math.log10(peak / 32768)).toFixed(1) + ' dBFS' : 'digital silence';
      add(card, 'p', 'Sent peak ' + level + ' at ' + signal.gain_db + ' dB gain · ' +
        signal.vad_voice_packets + ' VAD voice packets · ' + signal.clipped_samples + ' overloaded samples.');
    }
    if (result.speech_endpoint?.status === 'estimated') {
      const timing = result.speech_endpoint;
      add(card, 'p', 'Estimated speech end to audible reply: ' +
        (timing.speech_end_to_audible_ms == null ? 'unavailable' : timing.speech_end_to_audible_ms + ' ms') +
        ' · Input queue at estimated speech end: ' + timing.speech_end_to_send_ms + ' ms.');
    } else if (result.speech_endpoint) {
      add(card, 'p', 'Speech-end estimate unavailable: ' + result.speech_endpoint.reason);
    }
    if (result.warm_reply) {
      const warm = result.warm_reply;
      const timing = warm.speech_endpoint;
      add(card, 'p', 'Warm speech end to audible reply: ' +
        (timing?.speech_end_to_audible_ms == null ? 'unavailable' : timing.speech_end_to_audible_ms + ' ms') +
        (warm.error ? ' · ' + warm.error : ''));
      if (warm.input_queue?.max_packet_age_ms != null) {
        add(card, 'p', 'Warm input maximum queue age: ' + warm.input_queue.max_packet_age_ms + ' ms.');
      }
      if (timing?.speech_end_send_to_cloud_voice_ms != null) {
        add(card, 'p', 'Warm reply: ' + timing.speech_end_send_to_cloud_voice_ms +
          ' ms from sending the estimated speech end to receiving voice audio.');
      }
    }
    if (result.connection_start === 'before_wake' && result.wake_receipt_to_ready_ms != null) {
      const lead = result.wake_receipt_to_ready_ms;
      add(card, 'p', 'GPT-Live ready ' + Math.abs(lead) + ' ms ' + (lead <= 0 ? 'before' : 'after') + ' wake receipt.');
    }
    if (result.cloud?.transport) add(card, 'p', result.cloud.transport + ' / ' + result.cloud.profile + ' startup: ' +
      (result.cloud.startup_ms == null ? 'unavailable' : result.cloud.startup_ms + ' ms'));
    for (const trial of result.trials) {
      const cold = trial.cold_startup_ms;
      add(card, 'p', (trial.turn === 'warm_followup' ? 'Warm follow-up' : trial.turn === 'first' ? 'First reply' : 'Trial ' + trial.trial) + ' / ' +
        (trial.latency_ms == null ? (trial.reason || 'Acoustic measurement unavailable') :
          trial.latency_ms + (result.kind === 'voice_turn' ? ' ms from returned voice to audible output' : ' ms')) +
        (cold == null ? '' : ' / first sound ' + cold + ' ms after command'));
    }
    if (result.playback) add(card, 'p', result.playback.frames_accepted + ' frames accepted by the speaker API.');
    if (result.kind === 'reply_stall' && result.playback?.schedule_rebases) {
      add(card, 'p', 'Sender shifted its schedule by ' + result.playback.schedule_shift_ms + ' ms.');
    }
    if (result.stability) add(card, 'p', result.stability.status === 'measured' ?
      'Pilot gaps ' + result.stability.dropout_count : result.stability.reason);
    if (result.render_alignment) add(card, 'p', result.render_alignment.status === 'processed' ?
      'AEC3 processed both confirmed render bursts and the gap between them.' : result.render_alignment.reason);
    if (result.opening_prefix_echo) {
      const prefix = result.opening_prefix_echo;
      add(card, 'p', prefix.status === 'compared' ?
        'The early speaker report arrived ' + prefix.first_report_to_terminal_ms + ' ms before the final report and covered ' +
          prefix.overlapping_capture_ms + ' ms of microphone audio during playback.' :
        'Early echo comparison unavailable. ' + prefix.reason);
    }
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
for (const id of ['volume-confirmed', 'idle-confirmed', 'wake-startup-transport', 'voice-turn-confirmed', 'voice-standby']) $(id).addEventListener('change', readiness);

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
$('run-test').addEventListener('click', () => {
  const startup = kind === 'wake_ack' && $('wake-startup-transport').value;
  if (startup && !window.confirm('Start one budgeted GPT-Live startup test on the next wake? Room microphone audio stays local.')) return;
  const body = {kind, comfortable_volume_confirmed: $('volume-confirmed').checked};
  if (startup) Object.assign(body, {startup_transport: startup, confirm_paid: true});
  if (kind === 'voice_turn') {
    if (!$('voice-turn-confirmed').checked) return;
    $('voice-turn-confirmed').checked = false;
    const standby = $('voice-standby').checked;
    Object.assign(body, {allow_room_audio_to_openai: true, confirm_paid: true, standby,
      allow_repeated_wake_audio: standby, input_gain_db: Number($('voice-input-gain').value),
      follow_up: standby || $('voice-follow-up').checked, preconnect: !standby && $('voice-preconnect').checked});
  }
  action(() => api('api/tests', 'POST', body));
});
$('cancel-test').addEventListener('click', () => action(() => api('api/tests/current', 'DELETE')));
$('cloud-form').addEventListener('submit', (event) => {
  event.preventDefault();
  action(async () => {
    await api('api/cloud', 'PUT', {api_key: $('openai-key').value});
    $('openai-key').value = '';
  });
});

$('assist-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = $('assist-token');
  const result = $('assist-result');
  const button = $('assist-check');
  const token = input.value.trim();
  input.value = '';
  button.disabled = true;
  result.textContent = 'Checking Assist connection…';
  try {
    const {tool_count} = await api('api/assist/check', 'POST', {token});
    result.textContent = `Connected to Assist. ${tool_count} tool${tool_count === 1 ? '' : 's'} available.`;
  } catch (error) {
    result.textContent = error.message;
  } finally {
    button.disabled = false;
  }
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

$('run-synthetic-replies').addEventListener('click', () => {
  if (!window.confirm('Start one budgeted GPT-Live session with two built-in synthetic prompts? Room audio stays local.')) return;
  action(() => api('api/cloud/tests', 'POST', {transport: $('reply-transport').value,
    comfortable_volume_confirmed: $('volume-confirmed').checked, profile: $('reply-profile').value, confirm_paid: true}));
});

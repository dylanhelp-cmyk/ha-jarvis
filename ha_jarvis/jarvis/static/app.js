'use strict';

const $ = (id) => document.getElementById(id);
const labels = {latency: 'Playback latency', stop: 'Stop response', interrupt: 'Interruption check', stability: 'Stream stability',
  interrupt_speech: 'Spoken reply interruption',
  interrupt_acoustic: 'Acoustic interruption check',
  interrupt_control: 'Playback-only interruption check', synthetic_interrupt: 'Generated interruption control',
  reply_stall: 'Speech recovery', synthetic_replies: 'Synthetic reply timing', synthetic_sessions: 'Conversation checks', wake_ack: 'Wake acknowledgment', render_alignment: 'Echo reference alignment', duplex_stability: 'Conversation playback stability', voice_turn: 'Voice preview', echo: 'Quiet-room echo test', connection: 'Microphone connection'};
let kind = 'latency';
let state;
let busy = false;
let listenedRender;
let acousticSource;

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

function speakerRoute(device) {
  return device?.output_route ?? (device?.jack_plugged ? 'voice_pe_jack' : 'unavailable');
}

function readiness() {
  const device = state?.device;
  const running = Boolean(state?.test.running);
  const speaker = kind !== 'connection';
  const wakeTest = kind === 'wake_ack';
  const startup = $('wake-startup-transport');
  $('wake-startup-options').hidden = !wakeTest;
  $('voice-turn-consent').hidden = kind !== 'voice_turn';
  const acoustic = kind === 'interrupt_acoustic';
  $('interruption-source-options').hidden = !acoustic && kind !== 'interrupt_speech';
  $('separate-speaker-option').hidden = !acoustic;
  startup.disabled = running || busy;
  const assist = kind === 'voice_turn' && $('voice-assist').checked;
  const standby = kind === 'voice_turn' && $('voice-standby').checked;
  const reservations = state?.assist_model_options?.[state.config.assist_model];
  const remaining = state ? state.cloud.budget_usd - state.cloud.spent_usd : 0;
  const selected = state?.assist_model_options?.[$('assist-model').value];
  $('assist-sample').disabled = running || busy || !state?.cloud.api_key_configured || state.cloud.paid_retry_blocked ||
    !selected || selected.standby > remaining + 1e-12;
  if (selected) {
    $('assist-budget').textContent = 'Jarvis reserves up to US$' + selected.preview.toFixed(2) +
      ' for one preview or US$' + selected.standby.toFixed(2) + ' per standby wake. US$' +
      remaining.toFixed(2) + ' remains. The ledger settles after each session.';
  }
  $('voice-assist-fields').hidden = !assist;
  $('voice-assist-target').hidden = !assist || standby;
  $('voice-assist-mode').textContent = standby
    ? 'Allow control of Assist-exposed devices while wake standby is enabled.'
    : 'Allow one on or off command for a named Assist-exposed light or switch during this preview.';
  $('voice-context-options').hidden = !standby;
  $('voice-remember-context').disabled = running || busy;
  $('voice-preconnect').disabled = standby || assist;
  $('voice-follow-up').disabled = standby || assist;
  let reason = '';
  if (!device?.connected) reason = 'Connect the paired Voice PE.';
  else if (device.muted || !device.raw_ready) reason = 'Unmute Voice PE and wait for raw microphone verification.';
  else if (speaker && (device.playback_protocol !== 1 || speakerRoute(device) === 'unavailable')) reason = 'Install compatible Voice PE firmware or connect the 3.5 mm jack.';
  else if (speaker && (!$('volume-confirmed').checked || !$('idle-confirmed').checked)) reason = 'Confirm the speaker volume and idle state.';
  else if (kind.startsWith('interrupt') && !state.echo_alignment_ready) reason = 'Run Echo reference alignment before the interruption check.';
  else if (kind.startsWith('interrupt_') && !state.interruption_profile_ready) reason = 'Repeat Echo reference alignment in a quiet room.';
  else if (acoustic && !$('separate-speaker-confirmed').checked) reason = 'Confirm the separate speaker for browser audio.';
  else if (['render_alignment', 'duplex_stability'].includes(kind) && !state?.acknowledgment.verified) reason = 'Verify the wake clip before testing render alignment.';
  else if (kind === 'wake_ack' && !state?.acknowledgment.cached_on_device) reason = 'Verify and preload the wake acknowledgment first.';
  else if (wakeTest && startup.value && (!state?.cloud.api_key_configured || state.cloud.paid_retry_blocked)) reason = 'A configured OpenAI key and a settled budget ledger are required.';
  else if (kind === 'voice_turn' && (!state.echo_alignment_ready || !state.acknowledgment.cached_on_device)) reason = 'Run a successful Echo reference alignment first.';
  else if (kind === 'voice_turn' && $('voice-duplex').checked && !state.continuous_input_ready) reason = 'Repeat Echo reference alignment until the continuous microphone check passes.';
  else if (kind === 'voice_turn' && (!state.cloud.api_key_configured || state.cloud.paid_retry_blocked)) reason = 'A configured OpenAI key and a settled budget ledger are required.';
  else if (assist && reservations?.[standby ? 'standby' : 'preview'] > remaining + 1e-12) reason = 'The saved Assist model exceeds the available budget for this mode. Choose another model under Advanced home control.';
  else if (assist && !$('voice-assist-token').value.trim()) reason = 'Enter a local Home Assistant token.';
  else if (assist && !standby && !$('voice-assist-name').value.trim()) reason = 'Enter the selected device name.';
  else if (kind === 'voice_turn' && !$('voice-turn-confirmed').checked) reason = 'Review and authorize microphone audio for the selected voice mode.';
  $('readiness').textContent = running ? state.test.phase : reason || (kind === 'voice_turn' ? 'Ready for an authorized cloud voice preview.' : 'Ready for a local test.');
  $('run-test').textContent = standby ? 'Enable wake standby' : assist ? 'Run Assist voice preview' : 'Run ' + labels[kind].toLowerCase();
  $('cancel-test').textContent = state?.test.standby ? 'Stop wake standby' : 'Stop test';
  $('run-test').disabled = Boolean(reason || running || busy);
  $('cancel-test').hidden = !running;
  $('cancel-test').disabled = busy;
  $('save-config').disabled = running || busy || !$('jarvis-voice').value;
  $('run-session-checks').disabled = running || busy || !state?.cloud.api_key_configured || state.cloud.paid_retry_blocked;
  $('run-interruption-check').disabled = running || busy || !state?.cloud.api_key_configured || state.cloud.paid_retry_blocked;
  const ack = state?.acknowledgment;
  $('verify-ack').disabled = running || busy || !ack?.playback_id || listenedRender !== ack.playback_id;
  $('run-synthetic-replies').disabled = running || busy || !device?.connected || device.muted ||
    !device.raw_ready || device.playback_protocol !== 1 || speakerRoute(device) === 'unavailable' ||
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
  const route = speakerRoute(device);
  $('route-status').textContent = route === 'voice_pe_jack' ? 'Voice PE 3.5 mm' :
    route === 'voice_pe_internal' ? 'Voice PE internal speaker' : 'Speaker unavailable';
  $('route-detail').textContent = !device.connected ? 'Waiting for device' :
    route === 'voice_pe_internal' ? 'Jack unplugged' : route === 'voice_pe_jack' ? 'Jack connected' :
    'Check firmware or connect the 3.5 mm jack';
  $('test-status').textContent = data.test.running ? 'Running' : 'Standby';
  const activeGain = data.test.input_gain_db;
  $('test-detail').textContent = data.test.phase + (activeGain == null ? '' :
    ' · ' + activeGain + ' dB App mic gain' +
    (device.capture_gain ? ' · ' + device.capture_gain + '× firmware mic gain' : ''));
  $('version').textContent = 'App ' + data.version;
  $('cloud-state').textContent = (data.cloud.api_key_configured ? 'Key saved locally.' : 'No key saved.') +
    ' API test cap is US$' + data.cloud.budget_usd + '.';
  const ack = data.acknowledgment;
  const preview = $('ack-preview');
  if (preview.dataset.render !== (ack.playback_id || '')) {
    preview.dataset.render = ack.playback_id || '';
    preview.dataset.clip = ack.candidate_id || '';
    listenedRender = undefined;
    if (ack.candidate_id) preview.src = 'api/acknowledgment/preview?' +
      new URLSearchParams({clip: ack.candidate_id, render: ack.playback_id});
    else preview.removeAttribute('src');
  }
  preview.hidden = !ack.cached_on_host;
  $('verify-ack').hidden = !ack.cached_on_host || ack.verified;
  $('generate-ack').hidden = ack.verified;
  $('generate-ack').textContent = ack.cached_on_host ? 'Replace clip (paid API test)' : 'Generate clip (paid API test)';
  const ackStatus = ack.cached_on_device ? 'Verified clip cached on Voice PE.' :
    ack.phase === 'preload_failed' ? 'Verified clip saved. Upload to the Voice PE failed.' :
    ack.verified && data.device.connected && (data.device.ack_cache_max_frames || 48000) <= 48000 ?
      'Verified clip saved. Update the Voice PE to firmware 0.2.6 to cache the longer greeting.' :
    ack.verified ? 'Verified clip saved. Waiting for it to upload to the Voice PE.' :
    ack.cached_on_host ? 'Play the clip and confirm it says “Yes sir. How can I help?”' :
    ack.phase === 'failed' ? 'Generation failed. Check the API budget and key before retrying.' :
    'Generate the wake clip for this voice.';
  $('ack-state').textContent = ack.voice + ' · ' + ackStatus;
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
    add(card, 'h3', (result.kind === 'assist_sample' ? 'Sample light' : labels[result.kind] || result.kind) + ' / ' + result.status);
    if (result.assist_model) add(card, 'p', 'Assist reasoning model · ' + result.assist_model);
    if (result.assist_sample) {
      const sample = result.assist_sample;
      for (const [key, label] of Object.entries({verified_change: 'Verified light change', spoken_on: 'Spoke confirmed on state',
        fresh_read: 'Read changed state', spoken_off: 'Spoke confirmed off state'})) {
        const value = sample.checks[key];
        add(card, 'p', label + ' — ' + (value === undefined ? 'unavailable' : value ? 'passed' : 'failed'));
      }
      if (sample.failure) add(card, 'p', sample.failure);
      if (sample.ledger_cost_usd != null) add(card, 'p', 'Session ledger cost · US$' + sample.ledger_cost_usd.toFixed(4));
      for (const [i, ms] of sample.generated_speech_end_to_returned_voice_ms.entries()) {
        if (ms !== null) add(card, 'p', 'Turn ' + (i + 1) + ' · generated speech end to returned voice ' + ms + ' ms');
      }
    }
    if (result.error || result.reason) add(card, 'p', result.error || result.reason);
    if (result.interruption_checks) {
      const checks = result.interruption_checks;
      for (const [name, label] of Object.entries({interrupted_during_count: 'Spoke over an active count',
        replacement_question_heard: 'New question recognized', replacement_answer_once: 'New question answered once',
        count_stayed_stopped: 'Count stayed stopped through the observation'})) {
        add(card, 'p', label + ' — ' + (checks.checks[name] === true ? 'passed' :
          checks.checks[name] === false ? 'failed' : 'unavailable'));
      }
      add(card, 'p', checks.scope);
    }
    if (result.session_checks) {
      const checks = result.session_checks;
      const names = {first_answer: 'Opening answer', complete_exchange_available: 'Temporary context',
        resumed_answer: 'Answer using previous context', thanks_recognized: 'Thanks recognized',
        thanks_stays_open: 'Thanks keeps the conversation open', clear_signoff: 'Clear sign-off recognized'};
      for (const [name, label] of Object.entries(names)) {
        add(card, 'p', label + ' — ' + (checks.checks[name] === true ? 'passed' :
          checks.checks[name] === false ? 'failed' : 'unavailable'));
      }
      if (checks.failure) add(card, 'p', checks.failure);
      add(card, 'p', checks.scope);
    }
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
    const greeting = result.wake_acknowledgment;
    const wakeReady = result.wake_detection_to_ready_ms;
    if (greeting) {
      add(card, 'p', greeting.status === 'detected' ?
        'Cached greeting became audible ' + greeting.latency_ms + ' ms after wake detection.' :
        'Cached greeting timing unavailable. ' + greeting.reason);
    }
    if (wakeReady != null) {
      add(card, 'p', 'GPT-Live was ready ' + Math.abs(wakeReady) + ' ms ' +
        (wakeReady < 0 ? 'before' : 'after') + ' wake detection.');
      if (greeting?.estimated_end_ms != null) {
        const gap = Math.round(wakeReady - greeting.estimated_end_ms);
        add(card, 'p', 'GPT-Live was ready about ' + Math.abs(gap) + ' ms ' +
          (gap > 0 ? 'after' : 'before') + ' the estimated end of the greeting.');
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
    const warmTurns = result.warm_reply ? [result.warm_reply, ...(result.additional_warm_replies || [])] : [];
    for (const warm of warmTurns) {
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
    if (result.conversation_end === 'inactivity') add(card, 'p', 'Conversation closed after ' +
      (result.standby_idle_seconds ?? 10) + ' seconds without detected speech.');
    if (result.conversation_end === 'signoff') add(card, 'p', 'Conversation closed after your sign-off.');
    if (result.resumed_context_exchanges) add(card, 'p', 'Used recent context from the previous wake.');
    if (result.conversation_end === 'session_limit') add(card, 'p', 'Conversation reached the preview session limit.');
    if (wakeReady == null && result.connection_start === 'before_wake' && result.wake_receipt_to_ready_ms != null) {
      const lead = result.wake_receipt_to_ready_ms;
      add(card, 'p', 'GPT-Live ready ' + Math.abs(lead) + ' ms ' + (lead <= 0 ? 'before' : 'after') + ' wake receipt.');
    }
    if (result.cloud?.transport) add(card, 'p', result.cloud.transport + ' / ' + result.cloud.profile + ' startup: ' +
      (result.cloud.startup_ms == null ? 'unavailable' : result.cloud.startup_ms + ' ms'));
    const startup = result.cloud_readiness || result.cloud;
    if (startup?.websocket_connect_ms != null) {
      add(card, 'p', 'WebSocket connected in ' + startup.websocket_connect_ms + ' ms. ' +
        (startup.session_ready_wait_ms == null ? 'GPT-Live readiness was unavailable.' :
          'GPT-Live took another ' + startup.session_ready_wait_ms + ' ms to become ready.'));
    }
    for (const trial of result.trials) {
      const cold = trial.cold_startup_ms;
      add(card, 'p', (trial.turn === 'warm_followup' ? 'Warm follow-up' : trial.turn === 'first' ? 'First reply' : 'Trial ' + trial.trial) + ' / ' +
        (trial.latency_ms == null ? (trial.reason || 'Acoustic measurement unavailable') :
          trial.latency_ms + (result.kind === 'voice_turn' ? ' ms from returned voice to audible output' :
            ['interrupt_speech', 'interrupt_control', 'interrupt_acoustic'].includes(result.kind) ? ' ms from render start to audible reply' : ' ms')) +
        (cold == null ? '' : ' / first sound ' + cold + ' ms after command'));
    }
    if (result.playback) add(card, 'p', result.playback.frames_accepted + ' frames accepted by the speaker API.');
    if (result.reply_output_signal?.status === 'measured') {
      const signal = result.reply_output_signal;
      const details = document.createElement('details');
      add(details, 'summary', 'Reply level measurements');
      add(details, 'p', signal.scope);
      add(details, 'p', 'Output gain ' + signal.output_gain_db + ' dB; peak ' + signal.peak_pcm +
        ' PCM; full-scale samples ' + signal.full_scale_samples + '.');
      for (const window of signal.windows) add(details, 'p', (window.start_ms / 1000).toFixed(1) +
        ' s: ' + (window.rms_dbfs == null ? 'silent PCM' : 'RMS ' + window.rms_dbfs + ' dBFS') +
        ', peak ' + window.peak_pcm + ' PCM.');
      card.append(details);
    }
    if (result.kind === 'reply_stall' && result.playback?.schedule_rebases) {
      add(card, 'p', 'Sender shifted its schedule by ' + result.playback.schedule_shift_ms + ' ms.');
    }
    if (result.stability) add(card, 'p', result.stability.status === 'measured' ?
      'Pilot gaps ' + result.stability.dropout_count : result.stability.reason);
    if (result.render_alignment) add(card, 'p', result.render_alignment.status === 'processed' ?
      'AEC3 processed both confirmed render bursts and the gap between them.' : result.render_alignment.reason);
    if (result.continuous_input) add(card, 'p', result.continuous_input.status === 'passed' ?
      'Continuous microphone check passed across both replies. Room audio stayed local.' :
      'Continuous microphone check unavailable. See diagnostics for details.');
    if (result.opening_prefix_echo) {
      const prefix = result.opening_prefix_echo;
      add(card, 'p', prefix.status === 'compared' ?
        'The early speaker report arrived ' + prefix.first_report_to_terminal_ms + ' ms before the final report and covered ' +
          prefix.overlapping_capture_ms + ' ms of microphone audio during playback.' :
        'Early echo comparison unavailable. ' + prefix.reason);
    }
    if (result.echo) add(card, 'p', result.echo.status === 'measured' ?
      'Offline pilot suppression ' + result.echo.pilot_band_reduction_db + ' dB' : result.echo.reason);
    const interrupt = result.interruption_probe;
    if (interrupt?.playback_candidate_windows != null) {
      add(card, 'p', 'Playback-only test flagged ' + interrupt.playback_candidate_windows +
        ' speech windows as possible interruptions.');
    }
    if (interrupt?.early_candidate_relative_to_injection_ms != null) {
      add(card, 'p', 'First stop candidate appeared ' + Math.abs(interrupt.early_candidate_relative_to_injection_ms) +
        ' ms before the injected test speech.');
    }
    if (interrupt?.injection_to_stop_request_ms != null) {
      add(card, 'p', 'A stop was requested ' + interrupt.injection_to_stop_request_ms + ' ms after speech injection.');
    }
    if (result.kind === 'interrupt_speech' && interrupt?.injection_to_stop_request_ms != null) {
      add(card, 'p', 'Acoustic stop time unavailable for spoken replies.');
    }
    if (interrupt?.near_end_source_after_render_ms != null) {
      add(card, 'p', 'The separate speaker phrase was heard ' + interrupt.near_end_source_after_render_ms +
        ' ms after the Voice PE began rendering.');
      if (interrupt.cleaned_opening) add(card, 'p', interrupt.cleaned_opening.status === 'detected'
        ? 'The beginning of the phrase was detected after echo cancellation.'
        : 'The beginning of the phrase could not be verified after echo cancellation.');
    }
    if (interrupt?.evidence_timing?.first_joint_decision_ms != null) {
      add(card, 'p', 'The detector qualified speech ' + interrupt.evidence_timing.first_joint_decision_ms +
        ' ms after the test phrase began.');
    }
    if (result.live_echo) add(card, 'p', result.live_echo.status === 'measured' ?
      'Live DMA-reference pilot suppression ' + result.live_echo.pilot_band_reduction_db + ' dB' :
      'Live echo measurement unavailable. ' + result.live_echo.reason);
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
for (const id of ['volume-confirmed', 'idle-confirmed', 'separate-speaker-confirmed', 'wake-startup-transport', 'voice-turn-confirmed', 'voice-standby', 'voice-assist']) $(id).addEventListener('change', readiness);
for (const id of ['voice-assist-name', 'voice-assist-token']) $(id).addEventListener('input', readiness);

$('config-form').addEventListener('submit', (event) => {
  event.preventDefault();
  action(async () => {
    const config = {ha_lan_url: $('ha-lan-url').value.trim(), repetitions: Number($('repetitions').value),
      standby_idle_seconds: Number($('standby-idle-seconds').value), voice: $('jarvis-voice').value,
      personality: $('jarvis-personality').value, voice_output_gain_db: Number($('jarvis-volume').value),
      assist_model: $('assist-model').value};
    const changedPlayback = state?.config.voice !== config.voice ||
      state?.config.voice_output_gain_db !== config.voice_output_gain_db;
    if ($('device-token').value) config.device_token = $('device-token').value;
    await api('api/config', 'PUT', config);
    $('save-state').textContent = changedPlayback ? 'Settings saved. Check the wake clip and rerun Echo reference alignment.' : 'Settings saved.';
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

async function stopAcousticSource(run = acousticSource) {
  if (acousticSource === run) acousticSource = undefined;
  if (run && run.context.state !== 'closed') await run.context.close();
}

async function playWhenCued(run, clip) {
  try {
    for (let count = 0; count < 36 && acousticSource === run; count++) {
      const {test} = await api('api/status');
      if (acousticSource !== run || !test.running || test.id !== run.id) break;
      if (test.acoustic_source_ready) {
        const source = run.context.createBufferSource();
        const volume = run.context.createGain();
        volume.gain.value = .28;
        source.buffer = clip;
        source.connect(volume).connect(run.context.destination);
        source.onended = () => { void stopAcousticSource(run).catch(() => {}); };
        source.start();
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 180));
    }
  } catch (error) {
    if (acousticSource === run) notice(error.message);
  }
  await stopAcousticSource(run);
}

async function startAcousticTest(body) {
  const context = new AudioContext();
  const run = {context};
  acousticSource = run;
  try {
    await context.resume();
    const response = await fetch('api/interruption-source/' + body.source_variant + '.wav', {cache: 'no-store'});
    if (!response.ok) throw new Error('The local test phrase could not be prepared.');
    const clip = await context.decodeAudioData(await response.arrayBuffer());
    const {test_id} = await api('api/tests', 'POST', body);
    run.id = test_id;
    void playWhenCued(run, clip).catch((error) => notice(error.message));
  } catch (error) {
    await stopAcousticSource(run);
    throw error;
  }
}

$('run-test').addEventListener('click', () => {
  const startup = kind === 'wake_ack' && $('wake-startup-transport').value;
  if (startup && !window.confirm('Start one budgeted GPT-Live startup test on the next wake? Room microphone audio stays local.')) return;
  const body = {kind, comfortable_volume_confirmed: $('volume-confirmed').checked};
  if (kind === 'interrupt_acoustic' || kind === 'interrupt_speech') {
    body.source_variant = Number($('interruption-source-variant').value);
  }
  if (kind === 'interrupt_acoustic') {
    body.confirm_separate_speaker = $('separate-speaker-confirmed').checked;
    action(() => startAcousticTest(body));
    return;
  }
  if (startup) Object.assign(body, {startup_transport: startup, confirm_paid: true});
  if (kind === 'voice_turn') {
    if (!$('voice-turn-confirmed').checked) return;
    $('voice-turn-confirmed').checked = false;
    const assist = $('voice-assist').checked;
    const standby = $('voice-standby').checked;
    Object.assign(body, {allow_room_audio_to_openai: true, confirm_paid: true, standby,
      allow_repeated_wake_audio: standby, remember_context: standby && $('voice-remember-context').checked,
      input_gain_db: Number($('voice-input-gain').value),
      follow_up: standby || (!assist && $('voice-follow-up').checked),
      after_greeting: $('voice-after-greeting').checked,
      duplex: $('voice-duplex').checked,
      preconnect: !assist && !standby && $('voice-preconnect').checked});
    $('voice-remember-context').checked = false;
    if (assist && standby) {
      body.allow_assist_standby = true;
      body.assist_standby_token = $('voice-assist-token').value.trim();
    } else if (assist) {
      body.allow_assist_power_action = true;
      body.assist_preview = {token: $('voice-assist-token').value.trim(),
        domain: $('voice-assist-domain').value, name: $('voice-assist-name').value.trim()};
    }
    if (assist) $('voice-assist-token').value = '';
  }
  action(() => api('api/tests', 'POST', body));
});
$('cancel-test').addEventListener('click', () => action(() =>
  Promise.all([stopAcousticSource(), api('api/tests/current', 'DELETE')])));
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
  const name = $('assist-lookup-name').value.trim();
  input.value = '';
  button.disabled = true;
  result.textContent = 'Checking Assist connection…';
  try {
    const {tool_count, lookup} = await api('api/assist/check', 'POST', name ? {token, name} : {token});
    if (!lookup) {
      result.textContent = `Connected to Assist. ${tool_count} tool${tool_count === 1 ? '' : 's'} available.`;
    } else if (lookup.outcome === 'verified') {
      const attrs = lookup.attributes || {};
      const values = [lookup.state + (attrs.unit_of_measurement ? ' ' + attrs.unit_of_measurement : '')];
      if (attrs.brightness_pct != null) values.push(`brightness ${attrs.brightness_pct}%`);
      if (attrs.temperature != null) values.push(`setpoint ${attrs.temperature}${attrs.temperature_unit || ''}`);
      if (attrs.current_temperature != null) values.push(`current temperature ${attrs.current_temperature}${attrs.temperature_unit || ''}`);
      result.textContent = `${name} — ${values.join(', ')}.`;
    } else {
      result.textContent = lookup.outcome === 'unknown' ? 'Assist could not complete the lookup.'
        : 'No unique device state confirmed. Check the name and its Assist exposure.';
    }
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
  $('standby-idle-seconds').value = config.standby_idle_seconds;
  $('jarvis-personality').value = config.personality;
  $('jarvis-volume').value = config.voice_output_gain_db;
  showVoiceVolume();
  $('device-token').value = config.device_token;
  $('save-state').textContent = 'Settings loaded.';
  await poll();
  for (const voice of state.voice_options) {
    $('jarvis-voice').add(new Option(voice[0].toUpperCase() + voice.slice(1), voice));
  }
  $('jarvis-voice').value = config.voice;
  for (const model of Object.keys(state.assist_model_options)) {
    $('assist-model').add(new Option(model, model));
  }
  $('assist-model').value = config.assist_model;
  readiness();
}

function showVoiceVolume() {
  $('jarvis-volume-db').value = $('jarvis-volume').value.replace('-', '−') + ' dB';
}
$('jarvis-volume').addEventListener('input', showVoiceVolume);
$('assist-model').addEventListener('change', readiness);
$('assist-sample').addEventListener('click', () => {
  const model = $('assist-model').value;
  const hold = state.assist_model_options[model].standby;
  if (!window.confirm('Run two generated requests with ' + model + ' and an in-memory light? The ledger reserves US$' +
      hold.toFixed(2) + '. The test uses no room audio or household devices.')) return;
  action(() => api('api/cloud/assist-sample', 'POST', {model, confirm_paid: true}));
});

initialize().catch((error) => notice(error.message));
setInterval(() => { if (!busy) poll().catch((error) => notice(error.message)); }, 1500);

$('ack-preview').addEventListener('ended', (event) => {
  listenedRender = event.currentTarget.dataset.render;
  readiness();
});
$('verify-ack').addEventListener('click', () => action(() => api('api/acknowledgment/verify', 'POST', {
  clip: $('ack-preview').dataset.clip, render: listenedRender, heard_exact_phrase: true
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

$('run-session-checks').addEventListener('click', () => {
  if (!window.confirm('Run two generated-speech sessions using up to US$0.10 of the API test budget?')) return;
  action(() => api('api/cloud/session-checks', 'POST', {confirm_paid: true}));
});

$('run-interruption-check').addEventListener('click', () => {
  if (!window.confirm('Check cloud interruption with generated speech using up to US$0.05? No microphone or speaker audio is used.')) return;
  action(() => api('api/cloud/interrupt-check', 'POST', {confirm_paid: true}));
});

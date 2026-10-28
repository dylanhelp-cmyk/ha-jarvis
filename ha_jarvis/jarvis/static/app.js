/* Local audio bench. All service URLs are relative to preserve HA ingress paths. */
'use strict';
const $ = (id) => document.getElementById(id);
const labels = {latency: 'Playback latency', stop: 'Stop response', stability: 'Stream stability'};
let status = null;
let dirty = false;
let configured = false;
let kind = 'latency';
let busy = false;
let polling = false;
let configLoaded = false;

async function api(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(path, {cache: 'no-store', signal: controller.signal, ...options, headers: {'Content-Type': 'application/json', ...options.headers}});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error?.message || body.error || body.message || `Request failed (${response.status}).`);
    return body;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('The bench took too long to respond. Check the connection and try again.');
    throw error;
  } finally {clearTimeout(timeout);}
}
function announce(message, error = false) {
  $('notice').textContent = String(message);
  $('notice').classList.toggle('error', error);
  $('notice').hidden = !message;
}
function configValues() {
  return {wiim_host: $('wiim-host').value.trim(), stream_base_url: $('stream-base-url').value.trim(), route: $('route').value, codec: $('codec').value, repetitions: 3, device_token: $('device-token').value.trim(), verify_tls: $('verify-tls').checked, slimproto_port: Number($('slimproto-port').value), slimproto_player_id: $('slimproto-player').value};
}
function updateReadiness() {
  const running = Boolean(status?.test?.running);
  const readyText = kind === 'stop' ? 'Ready for one local stop trial.' : 'Ready for three local onset trials.';
  let reason = readyText;
  if (!status) reason = 'Waiting for the bench connection.';
  else if (running) reason = 'Test in progress. You can stop it at any time.';
  else if (!configLoaded) reason = 'Connection settings are unavailable. Refresh this page to retry.';
  else if (dirty) reason = 'Save your changed connection settings first.';
  else if (!configured) reason = 'Save a WiiM address and reachable stream URL first.';
  else if (!status.device?.connected) reason = 'Connect the microphone device before testing.';
  else if (status.device?.muted) reason = 'Unmute the microphone device before testing.';
  else if (status.device?.raw_ready !== true) reason = 'Waiting for verified raw microphone / XMOS readiness.';
  else if (!status.transports?.[$('route').value]?.available) reason = 'The selected transport is not available.';
  else if ($('codec').value === 'flac' && status.codecs?.flac === false) reason = 'FLAC requires ffmpeg. Select WAV or install the supported encoder.';
  else if ($('route').value === 'slimproto' && !(status.transports?.slimproto?.clients || []).some((player) => player.connected && (!$('slimproto-player').value || player.player_id === $('slimproto-player').value))) reason = 'Connect the selected WiiM player to this SlimProto service first.';
  else if (!$('volume-confirmed').checked || !$('idle-confirmed').checked) reason = 'Confirm listening volume and speaker readiness above.';
  const ready = reason === readyText;
  $('readiness').textContent = reason;
  $('run-test').disabled = !ready || busy;
  $('run-test').textContent = `Run ${labels[kind].toLowerCase()} →`;
  $('run-test').hidden = running;
  $('cancel-test').hidden = !running;
  $('cancel-test').disabled = busy;
  $('save-config').disabled = busy || running || !configLoaded;
  document.querySelectorAll('#config-form input, #config-form select').forEach((el) => {el.disabled = running || busy;});
  const route = $('route').value;
  $('codec').querySelector('[value="wav"]').textContent = route === 'slimproto' ? 'PCM (uncompressed)' : 'WAV';
  $('transport-help').textContent = route === 'slimproto' ? 'Requires an available SlimProto service and connected player. Expand advanced transport settings for pairing.' : route === 'airplay' ? 'AirPlay is a baseline transport; receiver capability and credentials must be validated first.' : 'HTTP streams a local test signal to the WiiM.';
  $('tls-warning').hidden = $('verify-tls').checked;
  try {$('lms-host').textContent = new URL($('stream-base-url').value).hostname;} catch {$('lms-host').textContent = 'the HA address in your stream URL';}
}
function renderStatus(data) {
  status = data;
  $('connection-dot').className = 'dot online';
  $('connection-label').textContent = 'Bench connected';
  const device = data.device || {};
  $('device-status').textContent = device.connected ? (device.muted ? 'Connected · muted' : (data.test?.running ? 'Connected · capturing' : device.raw_ready ? 'Ready · microphone idle' : 'Connected · awaiting raw audio')) : 'Awaiting microphone';
  $('device-detail').textContent = device.connected ? [device.device_id, device.sample_rate ? `${device.sample_rate.toLocaleString()} Hz` : '', device.firmware ? `FW ${device.firmware}` : ''].filter(Boolean).join(' · ') : 'Flash firmware and pair your device';
  const route = data.config?.route || 'http';
  $('route-status').textContent = route === 'slimproto' ? 'SlimProto' : route === 'airplay' ? 'AirPlay baseline' : 'Native HTTP';
  const codecLabel = route === 'slimproto' && data.config?.codec === 'wav' ? 'PCM' : String(data.config?.codec || 'wav').toUpperCase();
  $('route-detail').textContent = `${codecLabel} · ${data.transports?.[route]?.available ? 'Available' : 'Unavailable'}`;
  $('test-status').textContent = data.test?.running ? 'Test in progress' : 'Standby';
  $('test-detail').textContent = data.test?.running ? (data.test.phase || 'Preparing signal') : 'No test starts automatically';
  $('version').textContent = `v${data.version || '—'} · Local audio milestone`;
  const slimOption = $('route').querySelector('[value="slimproto"]');
  slimOption.textContent = data.transports?.slimproto?.available ? 'SlimProto' : 'SlimProto (unavailable)';
  slimOption.disabled = !data.transports?.slimproto?.available;
  const airplayOption = $('route').querySelector('[value="airplay"]');
  airplayOption.disabled = !data.transports?.airplay?.available;
  airplayOption.textContent = airplayOption.disabled ? 'AirPlay (unavailable)' : 'AirPlay baseline';
  const slim = data.transports?.slimproto || {};
  const clients = Array.isArray(slim.clients) ? slim.clients : [];
  const selectedPlayer = $('slimproto-player').value;
  const playerOptions = [node('option', '', 'Automatic player')];
  playerOptions[0].value = '';
  clients.forEach((player) => {
    const option = node('option', '', `${player.name || player.player_id}${player.connected ? '' : ' (offline)'}`);
    option.value = player.player_id;
    playerOptions.push(option);
  });
  if (selectedPlayer && !clients.some((player) => player.player_id === selectedPlayer)) {
    const option = node('option', '', `${selectedPlayer} (not connected)`); option.value = selectedPlayer; playerOptions.push(option);
  }
  $('slimproto-player').replaceChildren(...playerOptions);
  $('slimproto-player').value = selectedPlayer;
  $('slimproto-status').textContent = slim.available ? `SlimProto service available · ${clients.filter((player) => player.connected).length} connected player(s).` : `SlimProto unavailable: ${slim.reason || 'save a stream URL to initialize the service.'}`;
  $('airplay-status').textContent = data.transports?.airplay?.available ? 'AirPlay baseline available.' : `AirPlay unavailable: ${data.transports?.airplay?.reason || 'receiver capability and credentials have not been validated.'}`;
  const flac = $('codec').querySelector('[value="flac"]');
  flac.disabled = data.codecs?.flac === false;
  flac.textContent = flac.disabled ? 'FLAC (ffmpeg required)' : 'FLAC';
  updateReadiness();
}
function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = String(text);
  return item;
}
function milliseconds(value) { return typeof value === 'number' && Number.isFinite(value) ? `${Math.round(value)} ms` : '—'; }
function renderResults(data) {
  const results = Array.isArray(data.results) ? data.results : [];
  if (!results.length) return;
  const container = $('results-list');
  container.replaceChildren();
  results.forEach((result) => {
    const card = node('article', 'result-card');
    const top = node('div', 'result-top');
    top.append(node('h3', '', labels[result.kind] || result.kind || 'Audio test'), node('span', 'tag', result.status === 'unavailable' ? 'Completed · measurement unavailable' : result.status || 'Unknown'));
    card.append(top);
    const when = new Date(result.started_at);
    const date = Number.isNaN(when.getTime()) ? 'Time unavailable' : when.toLocaleString();
    card.append(node('p', 'result-meta', `${date} · ${result.route === 'slimproto' ? 'SlimProto' : result.route === 'airplay' ? 'AirPlay' : 'HTTP'} / ${result.wire_format === 'pcm_s16le' ? 'PCM' : String(result.codec || '—').toUpperCase()}`));
    const metrics = node('div', 'metrics');
    const stopMetric = result.kind === 'stop';
    [[stopMetric ? 'Median stop → silence' : 'Median onset latency', milliseconds(result.summary?.median_ms)], ['95th percentile', milliseconds(result.summary?.p95_ms)], ['Valid trials', `${result.summary?.valid_trials ?? 0} / ${result.trials?.length ?? 0}`]].forEach(([label, value]) => {
      const metric = node('div'); metric.append(node('span', 'metric-label', label), node('span', 'metric-value', value)); metrics.append(metric);
    });
    card.append(metrics);
    if (result.capture) {
      const health = result.capture;
      const level = (value) => typeof value === 'number' ? `${value.toFixed(1)} dBFS` : value === null ? 'digital silence' : 'unavailable';
      card.append(node('p', 'result-meta', `Microphone: ${health.duration_seconds}s captured · RMS ${level(health.rms_dbfs)} · peak ${level(health.peak_dbfs)} · ${((health.clipped_fraction || 0) * 100).toFixed(2)}% clipped.`));
      if (health.signal === 'digital_silence') card.append(node('p', 'result-warning', 'Microphone frames contain only zeros. Increasing speaker volume cannot resolve digital silence.'));
    }
    if (result.error) card.append(node('p', 'result-warning', `Test error: ${result.error}`));
    if (result.interruption) {
      const interruption = result.interruption;
      card.append(node('p', 'result-meta', `Ended after ${interruption.elapsed_seconds ?? '—'}s · ${interruption.reason} · phase: ${interruption.phase || 'unknown'}.`));
      if (interruption.protocol_error || interruption.socket_error) card.append(node('p', 'result-warning', interruption.protocol_error || interruption.socket_error));
    }
    const firstColdStart = result.trials?.find((trial) => typeof trial.cold_startup_ms === 'number');
    if (firstColdStart) card.append(node('p', 'result-meta', `Cold start estimate: ${milliseconds(firstColdStart.cold_startup_ms)} · control command → sound, including the 100 ms probe lead-in.`));
    const estimates = (result.trials || []).map((trial) => trial.clock_uncertainty_ms).filter((value) => typeof value === 'number' && Number.isFinite(value));
    if (estimates.length) card.append(node('p', 'result-meta', `Clock/detector estimate: ±${Math.max(...estimates).toFixed(1)} ms. Microphone pipeline delay is uncalibrated; this is not a total uncertainty bound.`));
    if (result.kind === 'stability') {
      const continuity = result.stability;
      const missing = (result.trials || []).filter((trial) => trial.latency_ms == null).length;
      card.append(node('p', 'result-meta', `Missing onset probes: ${missing} / ${result.trials?.length || 0}.`));
      if (continuity?.status === 'measured') {
        card.append(node('p', 'result-meta', `Continuity: ${continuity.dropout_count ?? 0} dropout(s) · ${milliseconds(continuity.dropout_ms)} total · longest gap ${milliseconds(continuity.longest_gap_ms)} · observed ${milliseconds(continuity.observed_ms)}.`));
        if (continuity.method) card.append(node('p', 'result-meta', `Continuity method: ${continuity.method}`));
      } else card.append(node('p', 'result-warning', `Continuity unavailable: ${continuity?.reason || 'insufficient acoustic evidence.'}`));
    }
    if (result.trials?.length) {
      const table = node('table', 'trial-table');
      const caption = node('caption', 'skip', `${labels[result.kind] || 'Audio test'} trial measurements`); table.append(caption);
      const head = node('thead'); const row = node('tr');
      ['Trial', stopMetric ? 'Stop → silence' : 'Onset latency', 'Confidence', 'Outcome'].forEach((label) => {const cell = node('th', '', label); cell.scope = 'col'; row.append(cell);}); head.append(row); table.append(head);
      const body = node('tbody');
      result.trials.forEach((trial) => {
        const row = node('tr');
        const confidence = typeof trial.confidence === 'number' ? `${Math.round(trial.confidence * 100)}%` : '—';
        [trial.trial, milliseconds(trial.latency_ms), confidence, trial.reason || trial.status || 'Unavailable'].forEach((value) => row.append(node('td', '', value ?? '—')));
        body.append(row);
      }); table.append(body); card.append(table);
    }
    (result.warnings || []).forEach((warning) => card.append(node('p', 'result-warning', warning)));
    container.append(card);
  });
}
async function poll() {
  if (document.hidden || polling) return;
  polling = true;
  const requests = await Promise.allSettled([api('api/status'), api('api/results')]);
  if (requests[0].status === 'fulfilled') renderStatus(requests[0].value);
  else {
    status = null;
    $('connection-dot').className = 'dot error';
    $('connection-label').textContent = 'Bench unreachable · retrying';
    $('device-status').textContent = 'Status unavailable';
    $('device-detail').textContent = 'Checking connection every 2 seconds';
    updateReadiness();
  }
  if (requests[1].status === 'fulfilled') renderResults(requests[1].value);
  polling = false;
}
$('config-form').addEventListener('input', () => {dirty = true; $('save-state').textContent = 'Unsaved changes'; updateReadiness();});
$('config-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  busy = true; updateReadiness();
  try {
    const values = configValues();
    const stream = new URL(values.stream_base_url);
    if (!['http:', 'https:'].includes(stream.protocol)) throw new Error('Use an HTTP or HTTPS stream base URL.');
    await api('api/config', {method: 'PUT', body: JSON.stringify(values)});
    dirty = false; configured = Boolean(values.wiim_host && values.stream_base_url);
    $('save-state').textContent = 'Connection saved';
    announce('Connection saved. Set a comfortable volume before running a test.');
    await poll();
  } catch (error) {announce(error.message, true);}
  finally {busy = false; updateReadiness();}
});
document.querySelectorAll('.test-option').forEach((button) => button.addEventListener('click', () => {
  kind = button.dataset.kind;
  document.querySelectorAll('.test-option').forEach((option) => {option.classList.toggle('selected', option === button); option.setAttribute('aria-pressed', String(option === button));});
  updateReadiness();
}));
['volume-confirmed', 'idle-confirmed'].forEach((id) => $(id).addEventListener('change', updateReadiness));
$('run-test').addEventListener('click', async () => {
  if ($('run-test').disabled) return;
  busy = true; updateReadiness();
  try {
    await api('api/tests', {method: 'POST', body: JSON.stringify({kind, repetitions: kind === 'stop' ? 1 : 3, comfortable_volume_confirmed: true})});
    announce(`${labels[kind]} started. Keep Voice PE in its normal listening location and the room quiet.`);
    await poll();
  } catch (error) {announce(error.message, true);}
  finally {busy = false; updateReadiness();}
});
$('cancel-test').addEventListener('click', async () => {
  busy = true; updateReadiness();
  try {await api('api/tests/current', {method: 'DELETE'}); announce('Stop requested. Previous music playback is not restored automatically.'); await poll();}
  catch (error) {announce(error.message, true);}
  finally {busy = false; updateReadiness();}
});
$('reveal-token').addEventListener('click', () => {
  const show = $('device-token').type === 'password';
  $('device-token').type = show ? 'text' : 'password';
  $('reveal-token').textContent = show ? 'Hide' : 'Show';
  $('reveal-token').setAttribute('aria-pressed', String(show));
  $('reveal-token').setAttribute('aria-label', `${show ? 'Hide' : 'Show'} device token`);
});
$('copy-token').addEventListener('click', async () => {
  try {await navigator.clipboard.writeText($('device-token').value); announce('Device token copied. Keep it private.');}
  catch {announce('Clipboard is unavailable here. Use Show, then select and copy the token manually.', true);}
});
async function initialize() {
  try {
    const config = await api('api/config');
    for (const key of ['wiim_host', 'stream_base_url', 'route', 'codec', 'device_token', 'slimproto_port', 'slimproto_player_id']) {
      const field = document.querySelector(`[name="${key}"]`);
      if (config[key] !== undefined) {
        if (key === 'slimproto_player_id' && config[key]) {
          const option = node('option', '', config[key]); option.value = config[key]; field.append(option);
        }
        field.value = config[key];
      }
    }
    $('verify-tls').checked = config.verify_tls !== false;
    configLoaded = true; configured = Boolean(config.wiim_host && config.stream_base_url);
    $('save-state').textContent = configured ? 'Saved connection loaded' : 'Set up your connection';
  } catch (error) {$('save-state').textContent = 'Could not load settings'; announce(error.message, true);}
  await poll(); updateReadiness();
}
document.addEventListener('visibilitychange', () => {if (!document.hidden) poll();});
initialize();
setInterval(poll, 2000);

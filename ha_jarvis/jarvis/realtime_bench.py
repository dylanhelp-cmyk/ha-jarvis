"""A two-turn Realtime comparison. Only the synthetic benchmark can construct it."""
import base64

from av import AudioFrame, AudioResampler

from .live_session import LiveSession, LiveSessionError, RATE, VOICE


MODEL = "gpt-realtime-2.1-mini"
PCM_RATE = 24000
SILENCE_MS = 220


def resample(pcm, converter, input_rate):
    frame = AudioFrame(format="s16", layout="mono", samples=len(pcm) // 2)
    frame.planes[0].update(pcm)
    frame.sample_rate = input_rate
    return b"".join(block.to_ndarray().astype("<i2", copy=False).tobytes()
                    for block in converter.resample(frame))


def estimated_cost(usage):
    """Use undiscounted model rates. Missing breakdowns use the audio rate."""
    cost = 0
    for side, audio_rate, text_rate in (("input", 10, .60), ("output", 20, 2.40)):
        total = usage.get(f"{side}_tokens")
        details = usage.get(f"{side}_token_details") or {}
        audio = details.get("audio_tokens", total)
        if type(total) is not int or type(audio) is not int or not 0 <= audio <= total:
            raise ValueError("Invalid Realtime usage")
        cost += (audio * audio_rate + (total - audio) * text_rate) / 1_000_000
    return cost


class RealtimeBenchmarkSession(LiveSession):
    def __init__(self, settings, *, on_audio, instructions, on_transcript):
        super().__init__(settings, on_audio=on_audio, transport="websocket",
                         instructions=instructions, on_transcript=on_transcript, output_buffer_seconds=8)
        self.metrics.transport = "realtime"
        self.metrics.model = MODEL
        self.input_converter = AudioResampler(format="s16", layout="mono", rate=PCM_RATE)
        self.output_converter = AudioResampler(format="s16", layout="mono", rate=RATE)
        self.armed = False
        self.requested = self.completed = 0
        self.cost_usd = 0.0

    async def _start_websocket(self):
        self._ws = await self._http.ws_connect(f"wss://api.openai.com/v1/realtime?model={MODEL}", max_msg_size=262144)
        self._spawn(self._receive_ws())
        audio_format = {"type": "audio/pcm", "rate": PCM_RATE}
        await self._ws.send_json({"type": "session.update", "session": {
            "type": "realtime", "instructions": self.instructions, "output_modalities": ["audio"],
            "reasoning": {"effort": "minimal"}, "max_output_tokens": 512,
            "audio": {"input": {"format": audio_format, "turn_detection": {
                "type": "server_vad", "silence_duration_ms": SILENCE_MS,
                "create_response": False, "interrupt_response": False}},
                "output": {"format": audio_format, "voice": VOICE}},
        }})

    def arm_turn(self):
        if self.armed or self.requested >= 2:
            raise LiveSessionError("Realtime benchmark allows exactly one response per synthetic turn")
        self.armed = True

    def reply_finished(self, turn):
        return self.completed >= turn and self._output.empty()

    async def _request_reply(self):
        try:
            await self._ws.send_json({"type": "response.create"})
        except Exception:
            self._fail("Realtime response request failed")

    def _event(self, event):
        if self._closing:
            return
        kind = event.get("type")
        if kind == "session.updated":
            super()._event({"type": "session.started", "session": event["session"]})
        elif kind == "input_audio_buffer.committed" and self.armed:
            self.armed = False
            self.requested += 1
            self._spawn(self._request_reply())
        elif kind == "response.output_audio.delta":
            pcm = base64.b64decode(event["delta"], validate=True)
            self._queue_audio(resample(pcm, self.output_converter, PCM_RATE))
        elif kind == "response.output_audio_transcript.delta" and self.on_transcript:
            self.on_transcript(event["delta"])
        elif kind == "response.done":
            if self.completed >= self.requested:
                self._fail("Unexpected Realtime response completion")
                return
            response = event["response"]
            for block in self.output_converter.resample(None):
                self._queue_audio(block.to_ndarray().astype("<i2", copy=False).tobytes())
            self.output_converter = AudioResampler(format="s16", layout="mono", rate=RATE)
            self.cost_usd += estimated_cost(response["usage"])
            self.completed += 1
            if response.get("status") != "completed":
                self._fail("Realtime response did not complete")
        elif kind == "error":
            code = event.get("error", {}).get("code")
            known_codes = {"invalid_request_error", "invalid_value", "rate_limit_exceeded",
                           "response_in_progress", "server_error", "insufficient_quota"}
            self._fail(f"Realtime benchmark reported {code}" if code in known_codes else
                       "Realtime benchmark reported an error")

    async def send_audio(self, pcm):
        if not self._ready.is_set() or self._closing or self._failed.is_set():
            raise LiveSessionError("Realtime session is not accepting audio")
        if len(pcm) != 640 or self.metrics.input_samples + 320 > 35 * RATE:
            raise LiveSessionError("Realtime benchmark input exceeded its fixed sample limit")
        converted = resample(pcm, self.input_converter, RATE)
        if converted:
            await self._ws.send_json({"type": "input_audio_buffer.append",
                                      "audio": base64.b64encode(converted).decode("ascii")})
        self.metrics.input_samples += 320

    async def _close(self):
        # Realtime usage arrives per response. Closing the socket stops the
        # experiment, and missing usage is charged to the local ledger at cap.
        self._final.set()
        metrics = await super()._close()
        if self._attempt and not metrics.finalization_confirmed:
            complete = self.requested == self.completed == 2
            self.settings.finalize_realtime(self._attempt, self.cost_usd if complete else None)
            metrics.finalization_confirmed = complete
        return metrics

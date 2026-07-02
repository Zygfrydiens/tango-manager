"""Gears Trainer -- metronome-paced checkpoint drill for the five abrazo gears.

A physical interface to con-embrace-states (data/concepts.json): the screen
calls a random gear (VIENTO / RAYO / AGUA / TIERRA / FUEGO), a metronome walks
a top-to-bottom scan of the gear's body stations (face -> toes), you set each
station on its beat, hold the finished gear for N beats, then the next gear is
called. The knobs are a fading scaffold:

    faster scan     raise BPM, or drop beats-per-checkpoint from 4 to 1
    less prompting  display modes: cues -> names only -> blind bars
    fewer stations  untick checkpoints you no longer need called
    faster switch   shrink the hold until the change is near-instant
    no fixed chain  shuffle randomizes the scan order

Endgame: all checkpoints off = pure shout-only gear switching (name -> body,
one move). Deliberately NO history, streaks, or session logs -- it remembers
knob positions (~/.tango_gears_trainer.json) and nothing else.

Station cues hand-extracted from con-embrace-states on 2026-07-02; keep them
in sync if the gear specs change. An em-dash cue means the gear spec is silent
for that station: keep it Viento-neutral, and if that feels wrong mid-drill,
that's a missing spec worth adding to the concept.

Sound: three woodblock-style clicks synthesized at startup -- accent (new
gear), checkpoint (a station lights), low quiet tick (sustain/hold beats) --
played through winmm waveOut, an ordinary app audio stream (falls back to
winsound.PlaySound, then the Tk bell). Click volume slider in the UI. To use
your own clicks, drop WAVs into sounds/ named gears_announce.wav /
gears_checkpoint.wav / gears_tick.wav (44.1 kHz 16-bit mono rides waveOut;
other formats fall back to PlaySound, at file volume).

Run:     py "scripts/gears_trainer.py"
Keys:    Space start/pause | N next gear | Up/Down tempo | F11 fullscreen
Smoke:   py "scripts/gears_trainer.py" --smoke        (headless self-test, ~7 s)
Silent?  py "scripts/gears_trainer.py" --sound-test   (system beep vs clicks)
"""

import array
import ctypes
import io
import json
import math
import random
import struct
import sys
import time
import tkinter as tk
import wave
from pathlib import Path

try:
    import winsound
except ImportError:  # non-Windows fallback -> Tk bell
    winsound = None

# ---------------------------------------------------------------- appearance

BG = "#0e1116"
PANEL = "#161b23"
TEXT = "#c7cfda"
MUTED = "#77828f"

ROW_PENDING_BG, ROW_PENDING_FG = "#1f2530", "#8b95a3"
ROW_DONE_BG, ROW_DONE_FG = "#173324", "#9ad7b0"
ROW_CURRENT_FG = "#101010"

STATUS_READ = "#c9d2de"
STATUS_SCAN = "#5f6b7a"
STATUS_HOLD = "#f5b942"

SETTINGS_FILE = Path.home() / ".tango_gears_trainer.json"
SOUNDS_DIR = Path(__file__).resolve().parent.parent / "sounds"

# Metronome clicks: woodblock-style, synthesized at startup (noise transient +
# two decaying sine partials). Drop your own WAVs in sounds/ to override:
# gears_announce.wav / gears_checkpoint.wav / gears_tick.wav.
SR = 44100

CLICK_SPECS = {              # freq Hz, amplitude 0..1, decay tau s
    "announce": (1760, 1.00, 0.016),     # new gear called (accent)
    "checkpoint": (1245, 0.95, 0.012),   # a station lights
    "tick": (880, 0.55, 0.010),          # sustain / hold beats (quieter)
}


def _synth_click(freq, amp, tau, vol=1.0, dur=0.085):
    rng = random.Random(7)  # deterministic transient
    frames = bytearray()
    for i in range(int(SR * dur)):
        t = i / SR
        env = math.exp(-t / tau)
        s = math.sin(2 * math.pi * freq * t) \
            + 0.4 * math.sin(2 * math.pi * freq * 2.41 * t)
        if t < 0.002:  # the 'click' of the click
            s += 2.2 * (rng.random() * 2 - 1) * (1 - t / 0.002)
        if t < 0.0004:  # sub-ms attack ramp, avoids a DC pop
            s *= t / 0.0004
        v = max(-1.0, min(1.0, amp * env * s / 1.9)) * vol
        frames += struct.pack("<h", int(v * 32767))
    return bytes(frames)


def _pcm_to_wav(pcm):
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(pcm)
    w.close()
    return buf.getvalue()


def _scale_pcm(pcm, vol):
    a = array.array("h")
    a.frombytes(pcm)
    return array.array("h", (int(x * vol) for x in a)).tobytes()


def _load_clicks(vol=1.0):
    """{kind: (raw_pcm_or_None, wav_bytes)} — pcm feeds waveOut, wav feeds PlaySound."""
    clicks = {}
    for kind, (freq, amp, tau) in CLICK_SPECS.items():
        custom = SOUNDS_DIR / ("gears_%s.wav" % kind)
        if custom.is_file():
            try:
                data = custom.read_bytes()
                pcm = None
                with wave.open(io.BytesIO(data)) as w:
                    if (w.getnchannels(), w.getsampwidth(),
                            w.getframerate()) == (1, 2, SR):
                        pcm = _scale_pcm(w.readframes(w.getnframes()), vol)
                clicks[kind] = (pcm, data)
                continue
            except Exception:
                pass
        pcm = _synth_click(freq, amp, tau, vol)
        clicks[kind] = (pcm, _pcm_to_wav(pcm))
    return clicks


class _WaveOutPlayer:
    """Clicks as raw PCM through winmm waveOut — an ordinary app audio stream,
    routed like any media app. winsound.PlaySound can ride a separately muted
    path (System Sounds), which is exactly the 'app looks fine, hears nothing'
    failure mode this sidesteps."""

    WAVE_MAPPER = 0xFFFFFFFF
    WHDR_DONE = 0x00000001

    class WAVEFORMATEX(ctypes.Structure):
        _fields_ = [("wFormatTag", ctypes.c_ushort), ("nChannels", ctypes.c_ushort),
                    ("nSamplesPerSec", ctypes.c_uint), ("nAvgBytesPerSec", ctypes.c_uint),
                    ("nBlockAlign", ctypes.c_ushort), ("wBitsPerSample", ctypes.c_ushort),
                    ("cbSize", ctypes.c_ushort)]

    class WAVEHDR(ctypes.Structure):
        _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", ctypes.c_uint),
                    ("dwBytesRecorded", ctypes.c_uint), ("dwUser", ctypes.c_void_p),
                    ("dwFlags", ctypes.c_uint), ("dwLoops", ctypes.c_uint),
                    ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_void_p)]

    def __init__(self):
        self.m = ctypes.WinDLL("winmm")
        fmt = self.WAVEFORMATEX(1, 1, SR, SR * 2, 2, 16, 0)  # PCM mono 16-bit
        self.h = ctypes.c_void_p()
        err = self.m.waveOutOpen(ctypes.byref(self.h), self.WAVE_MAPPER,
                                 ctypes.byref(fmt), None, None, 0)
        if err:
            raise OSError("waveOutOpen failed (%d)" % err)
        self.active = []  # (WAVEHDR, buffer) kept alive while playing

    def play(self, pcm):
        self._sweep()
        buf = ctypes.create_string_buffer(pcm, len(pcm))
        hdr = self.WAVEHDR()
        hdr.lpData = ctypes.cast(buf, ctypes.c_void_p)
        hdr.dwBufferLength = len(pcm)
        if self.m.waveOutPrepareHeader(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr)):
            return
        if self.m.waveOutWrite(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr)):
            self.m.waveOutUnprepareHeader(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr))
            return
        self.active.append((hdr, buf))

    def _sweep(self):
        keep = []
        for hdr, buf in self.active:
            if hdr.dwFlags & self.WHDR_DONE:
                self.m.waveOutUnprepareHeader(self.h, ctypes.byref(hdr),
                                              ctypes.sizeof(hdr))
            else:
                keep.append((hdr, buf))
        self.active = keep

    def close(self):
        try:
            self.m.waveOutReset(self.h)
            self._sweep()
            self.m.waveOutClose(self.h)
        except Exception:
            pass

# ------------------------------------------------------------------ the data
# Body stations, top -> bottom (the anatomical scan order).

STATIONS = [
    ("face", "Face & gaze", "Face"),
    ("shoulderblades", "Shoulderblades", "Shoulderblades"),
    ("arms", "Arms & elbows", "Arms"),
    ("left_hand", "Left hand (grip)", "Left hand"),
    ("core", "Core", "Core"),
    ("hips", "Hips", "Hips"),
    ("knees", "Knees & height", "Knees"),
    ("toes", "Toes & floor", "Toes"),
]

STATION_LABELS = {key: label for key, label, _ in STATIONS}

GEAR_ORDER = ["VIENTO", "RAYO", "AGUA", "TIERRA", "FUEGO"]

GEARS = {
    "VIENTO": {
        "color": "#b8c7d4",
        "subtitle": "Abrazo del Viento — standard · relaxed, cool · the home gear",
        "cues": {
            "face": "thinking & longing, touch of sorrow — gaze into the void",
            "shoulderblades": "relaxed",
            "arms": "relaxed",
            "left_hand": "default grip",
            "core": "relaxed",
            "hips": "neutral",
            "knees": "neutral",
            "toes": "neutral",
        },
    },
    "RAYO": {
        "color": "#ffd24a",
        "subtitle": "Abrazo del Rayo — strong · small, VERY dynamic · sharp / electric",
        "cues": {
            "face": "pained thought — 'seriously?!'",
            "shoulderblades": "pulled down AND forward — engaged, not locked",
            "arms": "elbows drawn in — spring between the elbows",
            "left_hand": "grip TIGHTER — connection + stopping power",
            "core": "ACTIVE",
            "hips": "—",
            "knees": "—",
            "toes": "—",
        },
    },
    "AGUA": {
        "color": "#53c1f0",
        "subtitle": "Abrazo del Agua — hip-engine · spiral · big AND small, controlled",
        "cues": {
            "face": "semi-sweet nostalgic sorrow — attention to HER",
            "shoulderblades": "released — the spiral travels all the way up",
            "arms": "—",
            "left_hand": "default grip",
            "core": "RELEASED — let the spiral through",
            "hips": "the ENGINE — hips drive the figure",
            "knees": "soft — weight down, body a little low",
            "toes": "—",
        },
    },
    "TIERRA": {
        "color": "#d9a05b",
        "subtitle": "Abrazo de la Tierra — milonguero · low, heart-to-heart · quiet-rhythmic",
        "cues": {
            "face": "bright, confident, playful smile — 'I will show you!'",
            "shoulderblades": "—",
            "arms": "right hand strong on her back — heart-to-heart",
            "left_hand": "barely present — low, little/no grip",
            "core": "—",
            "hips": "—",
            "knees": "soft — body lower",
            "toes": "—",
        },
    },
    "FUEGO": {
        "color": "#ff6a4d",
        "subtitle": "Abrazo del Fuego — wide + explosive · band-stretch · SHOW ONLY",
        "cues": {
            "face": "small cheeky smile — 'I know something others don't'",
            "shoulderblades": "apart, down AND forward — active, strong",
            "arms": "WIDE — stretch the elastic band, spring in the elbows",
            "left_hand": "grip TIGHTER",
            "core": "ACTIVE",
            "hips": "—",
            "knees": "legs push DOWN into the floor before the step",
            "toes": "EXAGGERATED — each step more active",
        },
    },
}


class GearsTrainer:
    def __init__(self, root, smoke=False):
        self.root = root
        self.smoke = smoke
        self.running = False
        self.after_id = None
        self.dot_after_id = None
        self.next_time = 0.0
        self.events = []
        self.ei = 0
        self.pending_apply = False
        self.gear = None
        self.current_key = None
        self.cycle_stations = []
        self.row_state = {}
        self.cycle_count = 0
        self.fullscreen = False
        self.smoke_ok = False

        # knobs
        self.bpm = tk.IntVar(value=60)
        self.bpc = tk.IntVar(value=1)      # beats per checkpoint
        self.hold = tk.IntVar(value=8)     # hold, in beats
        self.lead = tk.IntVar(value=2)     # read-the-name lead-in, in beats
        self.mode = tk.StringVar(value="cues")   # cues | names | blind
        self.shuffle_var = tk.BooleanVar(value=False)
        self.sound_var = tk.BooleanVar(value=True)
        self.vol = tk.IntVar(value=80)           # click volume, percent
        self.station_vars = {k: tk.BooleanVar(value=True) for k, _, _ in STATIONS}
        self.gear_vars = {g: tk.BooleanVar(value=True) for g in GEAR_ORDER}

        if not smoke:
            self._load_settings()
        self.clicks = _load_clicks(self._ival(self.vol, 80, 10, 100) / 100.0)
        self.player = None
        if winsound:  # windows -> try the waveOut backend first
            try:
                self.player = _WaveOutPlayer()
            except Exception:
                self.player = None
        self._build_ui()
        self._bind_keys()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------- UI

    def _build_ui(self):
        self.root.title("Gears Trainer — the five abrazos")
        self.root.configure(bg=BG)
        self.root.geometry("1080x880")
        self.root.minsize(900, 720)

        display = tk.Frame(self.root, bg=BG)
        display.pack(side="top", fill="both", expand=True)

        self.name_lbl = tk.Label(display, text="GEARS", font=("Segoe UI", 64, "bold"),
                                 bg=BG, fg=TEXT)
        self.name_lbl.pack(pady=(28, 0))
        self.subtitle_lbl = tk.Label(display, text="Space to start", font=("Segoe UI", 14),
                                     bg=BG, fg=MUTED)
        self.subtitle_lbl.pack()

        status_row = tk.Frame(display, bg=BG)
        status_row.pack(pady=(10, 14))
        self.dot = tk.Canvas(status_row, width=26, height=26, bg=BG, highlightthickness=0)
        self.dot_id = self.dot.create_oval(4, 4, 22, 22, fill="#2a3140", outline="")
        self.dot.pack(side="left", padx=(0, 12))
        self.status_lbl = tk.Label(status_row, text="", font=("Segoe UI", 24, "bold"),
                                   bg=BG, fg=STATUS_SCAN, width=12)
        self.status_lbl.pack(side="left")

        self.rows_holder = tk.Frame(display, bg=BG, width=860)
        self.rows_holder.pack(pady=(0, 12))
        self.rows = {}
        for key, label, _ in STATIONS:
            frame = tk.Frame(self.rows_holder, bg=ROW_PENDING_BG, height=52, width=860)
            frame.pack_propagate(False)
            name = tk.Label(frame, text=label, font=("Segoe UI", 17, "bold"),
                            bg=ROW_PENDING_BG, fg=ROW_PENDING_FG, anchor="w", width=16)
            name.pack(side="left", padx=(16, 10))
            cue = tk.Label(frame, text="", font=("Segoe UI", 12),
                           bg=ROW_PENDING_BG, fg=ROW_PENDING_FG, anchor="w")
            cue.pack(side="left", fill="x", expand=True, padx=(0, 12))
            self.rows[key] = (frame, name, cue)

        self._build_controls()
        self._layout_rows([k for k, _, _ in STATIONS])

    def _build_controls(self):
        lbl_style = dict(bg=PANEL, fg=MUTED, font=("Segoe UI", 10))
        chk_style = dict(bg=PANEL, fg=TEXT, font=("Segoe UI", 10),
                         activebackground=PANEL, activeforeground=TEXT,
                         selectcolor="#0b0e13", highlightthickness=0, anchor="w",
                         takefocus=0)
        spin_style = dict(width=4, font=("Segoe UI", 11), bg="#1d2430", fg=TEXT,
                          buttonbackground="#1d2430", insertbackground=TEXT, relief="flat",
                          highlightthickness=1, highlightbackground="#2a3140")

        panel = tk.Frame(self.root, bg=PANEL)
        panel.pack(side="bottom", fill="x")

        row1 = tk.Frame(panel, bg=PANEL)
        row1.pack(fill="x", padx=14, pady=(10, 2))
        self.start_btn = tk.Button(row1, text="▶  Start", width=10, command=self._toggle_run,
                                   font=("Segoe UI", 11, "bold"), bg="#2f6feb", fg="white",
                                   activebackground="#2458c5", activeforeground="white",
                                   relief="flat", cursor="hand2", takefocus=0)
        self.start_btn.pack(side="left", padx=(0, 18))
        tk.Label(row1, text="Tempo (BPM)", **lbl_style).pack(side="left")
        tk.Scale(row1, variable=self.bpm, from_=20, to=200, orient="horizontal", length=260,
                 bg=PANEL, fg=TEXT, troughcolor="#0b0e13", highlightthickness=0,
                 font=("Segoe UI", 9), activebackground=PANEL,
                 takefocus=0).pack(side="left", padx=(6, 18))
        for text, var, lo, hi in [("Beats / checkpoint", self.bpc, 1, 4),
                                  ("Hold (beats)", self.hold, 0, 64),
                                  ("Lead-in (beats)", self.lead, 0, 8)]:
            tk.Label(row1, text=text, **lbl_style).pack(side="left")
            tk.Spinbox(row1, textvariable=var, from_=lo, to=hi, **spin_style).pack(
                side="left", padx=(6, 18))

        row2 = tk.Frame(panel, bg=PANEL)
        row2.pack(fill="x", padx=14, pady=2)
        tk.Label(row2, text="Display", **lbl_style).pack(side="left")
        for text, val in [("cues", "cues"), ("names only", "names"), ("blind bars", "blind")]:
            tk.Radiobutton(row2, text=text, variable=self.mode, value=val,
                           command=self._refresh_texts, **chk_style).pack(side="left", padx=4)
        tk.Checkbutton(row2, text="shuffle scan order", variable=self.shuffle_var,
                       **chk_style).pack(side="left", padx=(18, 4))
        tk.Checkbutton(row2, text="sound", variable=self.sound_var,
                       command=lambda: self._click("checkpoint"), **chk_style).pack(
            side="left", padx=4)
        tk.Label(row2, text="vol", **lbl_style).pack(side="left", padx=(10, 0))
        vol_scale = tk.Scale(row2, variable=self.vol, from_=10, to=100,
                             orient="horizontal", length=90, showvalue=0,
                             bg=PANEL, fg=TEXT, troughcolor="#0b0e13",
                             highlightthickness=0, activebackground=PANEL, takefocus=0)
        vol_scale.pack(side="left", padx=(2, 4))
        vol_scale.bind("<ButtonRelease-1>", lambda e: self._set_volume())
        tk.Label(row2, text="Space start · N next · Up/Down tempo · F11 full",
                 **lbl_style).pack(side="right")

        row3 = tk.Frame(panel, bg=PANEL)
        row3.pack(fill="x", padx=14, pady=2)
        tk.Label(row3, text="Checkpoints", **lbl_style).pack(side="left")
        for key, _, short in STATIONS:
            tk.Checkbutton(row3, text=short, variable=self.station_vars[key],
                           **chk_style).pack(side="left", padx=3)

        row4 = tk.Frame(panel, bg=PANEL)
        row4.pack(fill="x", padx=14, pady=(2, 10))
        tk.Label(row4, text="Gears", **lbl_style).pack(side="left")
        for g in GEAR_ORDER:
            style = dict(chk_style)
            style["fg"] = GEARS[g]["color"]
            style["activeforeground"] = GEARS[g]["color"]
            tk.Checkbutton(row4, text=g.capitalize(), variable=self.gear_vars[g],
                           **style).pack(side="left", padx=3)

    def _bind_keys(self):
        def unless_typing(fn):
            def handler(event):
                if isinstance(event.widget, (tk.Spinbox, tk.Entry)):
                    return None  # let the spinbox keep its keys
                fn()
                return "break"
            return handler

        self.root.bind_all("<space>", unless_typing(self._toggle_run))
        self.root.bind_all("<KeyPress-n>", unless_typing(self._skip_gear))
        self.root.bind_all("<KeyPress-N>", unless_typing(self._skip_gear))
        self.root.bind_all("<Up>", unless_typing(lambda: self._nudge_bpm(2)))
        self.root.bind_all("<Down>", unless_typing(lambda: self._nudge_bpm(-2)))
        self.root.bind_all("<F11>", lambda e: self._toggle_fullscreen())
        self.root.bind_all("<Escape>", lambda e: self._set_fullscreen(False))
        # clicking the display area pulls focus out of a spinbox
        self.name_lbl.bind("<Button-1>", lambda e: self.root.focus_set())
        self.rows_holder.bind("<Button-1>", lambda e: self.root.focus_set())

    # ------------------------------------------------------------ the engine

    def _toggle_run(self):
        if self.running:
            self._pause()
        else:
            self._start()

    def _start(self):
        if not self.events or self.ei >= len(self.events):
            if not self._prepare_cycle():
                return
        self.running = True
        self.start_btn.config(text="⏸  Pause")
        self.next_time = time.perf_counter() + 0.4
        self.after_id = self.root.after(400, self._tick)

    def _pause(self):
        self.running = False
        if self.after_id:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        self.start_btn.config(text="▶  Start")
        self._set_status("paused", MUTED)

    def _prepare_cycle(self):
        enabled_gears = [g for g in GEAR_ORDER if self.gear_vars[g].get()]
        if not enabled_gears:
            self._pause()
            self._set_status("no gears on", STATUS_HOLD)
            return False
        pool = [g for g in enabled_gears if g != self.gear] or enabled_gears
        self.gear = random.choice(pool)

        stations = [k for k, _, _ in STATIONS if self.station_vars[k].get()]
        order = stations[:]
        if self.shuffle_var.get():
            random.shuffle(order)
        bpc = self._ival(self.bpc, 1, 1, 4)
        hold = self._ival(self.hold, 8, 0, 64)
        lead = self._ival(self.lead, 2, 0, 8)

        events = []
        for j in range(lead):
            events.append(("announce", j, lead))
        for i, key in enumerate(order, 1):
            events.append(("light", key, i, len(order)))
            events.extend([("tick",)] * (bpc - 1))
        for k in range(hold, 0, -1):
            events.append(("hold", k))
        if not events:
            events = [("announce", 0, 1)]

        self.events, self.ei = events, 0
        self.cycle_stations = stations
        self.pending_apply = True
        self.cycle_count += 1
        return True

    def _tick(self):
        if not self.running:
            return
        if self.pending_apply:
            self._apply_gear()
            self.pending_apply = False
        self._exec_event(self.events[self.ei])
        self._pulse_dot()
        self.ei += 1
        if self.ei >= len(self.events):
            if not self._prepare_cycle():
                return
        interval = 60.0 / self._ival(self.bpm, 60, 20, 300)
        self.next_time += interval
        now = time.perf_counter()
        if self.next_time < now + 0.005:  # resync after a stall or a big BPM jump
            self.next_time = now + interval
        self.after_id = self.root.after(
            max(1, int(round((self.next_time - now) * 1000))), self._tick)

    def _exec_event(self, ev):
        kind = ev[0]
        if kind == "announce":
            j, lead = ev[1], ev[2]
            left = lead - j
            self._set_status("READ · %d" % left if lead > 1 else "READ", STATUS_READ)
            self._click("announce")
        elif kind == "light":
            key, i, n = ev[1], ev[2], ev[3]
            if self.current_key is not None:
                self._paint(self.current_key, "done")
            self.current_key = key
            self._paint(key, "current")
            self._set_status("SCAN %d/%d" % (i, n), STATUS_SCAN)
            self._click("checkpoint")
        elif kind == "hold":
            if self.current_key is not None:
                self._paint(self.current_key, "done")
                self.current_key = None
            self._set_status("HOLD · %d" % ev[1], STATUS_HOLD)
            self._click("tick")
        else:  # plain sustain tick between checkpoints
            self._click("tick")

    def _apply_gear(self):
        g = GEARS[self.gear]
        self.name_lbl.config(text=self.gear, fg=g["color"])
        self.subtitle_lbl.config(
            text=g["subtitle"] if self.mode.get() == "cues" else "")
        self.current_key = None
        self._layout_rows(self.cycle_stations)

    def _skip_gear(self):
        if self._prepare_cycle() and not self.running:
            self._apply_gear()
            self.pending_apply = False

    # ---------------------------------------------------------------- visuals

    def _layout_rows(self, visible):
        for key, _, _ in STATIONS:
            self.rows[key][0].pack_forget()
        self.row_state = {}
        for key in visible:  # anatomical order; shuffle only changes lighting order
            self.rows[key][0].pack(fill="x", pady=3)
            self.row_state[key] = "pending"
            self._paint(key, "pending")

    def _paint(self, key, state):
        self.row_state[key] = state
        frame, name, cue = self.rows[key]
        if state == "current":
            bg, fg = GEARS[self.gear]["color"], ROW_CURRENT_FG
        elif state == "done":
            bg, fg = ROW_DONE_BG, ROW_DONE_FG
        else:
            bg, fg = ROW_PENDING_BG, ROW_PENDING_FG
        mode = self.mode.get()
        name_text = "" if mode == "blind" else STATION_LABELS[key]
        cue_text = ""
        if mode == "cues" and self.gear:
            cue_text = GEARS[self.gear]["cues"].get(key, "")
        frame.config(bg=bg)
        name.config(bg=bg, fg=fg, text=name_text)
        cue.config(bg=bg, fg=fg, text=cue_text)

    def _refresh_texts(self):
        for key, state in self.row_state.items():
            self._paint(key, state)
        if self.gear:
            self.subtitle_lbl.config(
                text=GEARS[self.gear]["subtitle"] if self.mode.get() == "cues" else "")

    def _set_status(self, text, color):
        self.status_lbl.config(text=text, fg=color)

    def _pulse_dot(self):
        self.dot.itemconfig(self.dot_id, fill="#e8edf5")
        if self.dot_after_id:
            self.root.after_cancel(self.dot_after_id)
        self.dot_after_id = self.root.after(
            110, lambda: self.dot.itemconfig(self.dot_id, fill="#2a3140"))

    def _click(self, kind):
        if not self.sound_var.get():
            return
        pcm, wav = self.clicks[kind]
        if self.player and pcm:
            self.player.play(pcm)
        elif winsound:
            try:
                winsound.PlaySound(
                    wav,
                    winsound.SND_MEMORY | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            except Exception:
                pass
        else:
            self.root.bell()

    def _set_volume(self):
        self.clicks = _load_clicks(self._ival(self.vol, 80, 10, 100) / 100.0)
        self._click("checkpoint")

    # ------------------------------------------------------------------ misc

    def _ival(self, var, default, lo, hi):
        try:
            v = int(var.get())
        except Exception:
            v = default
        return max(lo, min(hi, v))

    def _nudge_bpm(self, delta):
        v = self._ival(self.bpm, 60, 20, 200) + delta
        self.bpm.set(max(20, min(200, v)))

    def _toggle_fullscreen(self):
        self._set_fullscreen(not self.fullscreen)

    def _set_fullscreen(self, flag):
        self.fullscreen = flag
        self.root.attributes("-fullscreen", flag)

    def _load_settings(self):
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        for name, var in [("bpm", self.bpm), ("bpc", self.bpc), ("hold", self.hold),
                          ("lead", self.lead), ("mode", self.mode),
                          ("shuffle", self.shuffle_var), ("sound", self.sound_var),
                          ("volume", self.vol)]:
            if name in data:
                try:
                    var.set(data[name])
                except Exception:
                    pass
        for key, var in self.station_vars.items():
            var.set(bool(data.get("stations", {}).get(key, True)))
        for g, var in self.gear_vars.items():
            var.set(bool(data.get("gears", {}).get(g, True)))
        if self.mode.get() not in ("cues", "names", "blind"):
            self.mode.set("cues")

    def _save_settings(self):
        data = {
            "bpm": self._ival(self.bpm, 60, 20, 200),
            "bpc": self._ival(self.bpc, 1, 1, 4),
            "hold": self._ival(self.hold, 8, 0, 64),
            "lead": self._ival(self.lead, 2, 0, 8),
            "mode": self.mode.get(),
            "shuffle": self.shuffle_var.get(),
            "sound": self.sound_var.get(),
            "volume": self._ival(self.vol, 80, 10, 100),
            "stations": {k: v.get() for k, v in self.station_vars.items()},
            "gears": {g: v.get() for g, v in self.gear_vars.items()},
        }
        try:
            SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _on_close(self):
        self._pause()
        if self.player:
            self.player.close()
        if not self.smoke:
            self._save_settings()
        self.root.destroy()


def _sound_test():
    """Play each audio path in sequence so silence can be localized."""
    print("Gears Trainer sound test")
    print("------------------------")
    if not winsound:
        print("winsound unavailable (not Windows) -- the app would use the Tk bell.")
        return 0
    clicks = _load_clicks(1.0)
    print("[1/3] Windows system beep (MessageBeep)...")
    winsound.MessageBeep()
    time.sleep(1.0)
    print("[2/3] three clicks via winsound.PlaySound...")
    for kind in ("announce", "checkpoint", "tick"):
        winsound.PlaySound(clicks[kind][1], winsound.SND_MEMORY)
        time.sleep(0.30)
    print("[3/3] three clicks via winmm waveOut (the app's primary path)...")
    try:
        player = _WaveOutPlayer()
    except Exception as e:
        print("  waveOut unavailable: %s" % e)
        return 1
    for kind in ("announce", "checkpoint", "tick"):
        pcm = clicks[kind][0]
        if pcm:
            player.play(pcm)
        time.sleep(0.30)
    time.sleep(0.3)
    player.close()
    print()
    print("Heard [3] (maybe [2] too)  -> the app will click: check its sound box + vol slider.")
    print("Heard only [1]             -> PlaySound path muted; waveOut result decides.")
    print("Heard nothing at all       -> Windows side: default output device, and the")
    print("                              volume mixer (python.exe may be muted).")
    return 0


def main():
    if "--sound-test" in sys.argv:
        sys.exit(_sound_test())
    smoke = "--smoke" in sys.argv
    root = tk.Tk()
    app = GearsTrainer(root, smoke=smoke)
    if smoke:
        root.withdraw()
        app.sound_var.set(False)
        app.bpm.set(200)
        app.bpc.set(1)
        app.hold.set(1)
        app.lead.set(1)
        app._start()

        def finish():
            app.smoke_ok = app.cycle_count >= 3
            app._pause()
            root.destroy()

        root.after(7000, finish)
    root.mainloop()
    if smoke:
        print("SMOKE OK (cycles=%d)" % app.cycle_count if app.smoke_ok
              else "SMOKE FAIL (cycles=%d)" % app.cycle_count)
        sys.exit(0 if app.smoke_ok else 1)


if __name__ == "__main__":
    main()

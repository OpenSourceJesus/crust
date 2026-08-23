"""hostsim.py - drive a host-compiled bare-metal application from Python.

    import sys; sys.path.insert(0, "tools")
    from hostsim import Sim

    sim = Sim("/tmp/userapp.so")
    sim.start()
    while not sim.finished:
        sim.step_ms(1)
        print(sim.read(), end="")

Each :class:`Sim` is one simulated board. The application runs on its own
thread inside the shared object, and virtual time only advances when
:meth:`step` is called -- so the schedule is deterministic regardless of host
load, and several boards can be advanced in lockstep by stepping each in turn.

Because this is an ordinary host process, everything the host has is in reach
from the same loop: numpy on the sampled data, matplotlib to plot it, sockets
to the rest of a fleet, CUDA if the application was linked against it. That is
the point of the arrangement -- see :class:`Fleet` for the multi-board case.

Each board needs its own copy of the shared object. ``dlopen`` returns the
same handle for the same path, and the application's state is in ordinary
globals, so two boards loaded from one file would silently share a console and
a clock. :class:`Sim` copies the file per instance to avoid that.
"""

import ctypes
import os
import shutil
import tempfile

#: Default architected counter frequency, matching the Jetson and the Pi 3.
DEFAULT_FREQ = 19200000


class Sim:
    """One simulated board, backed by a host-compiled shared object."""

    def __init__(self, path, freq=DEFAULT_FREQ, name=None):
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        self.name = name or os.path.basename(path)
        self.freq = freq

        # A private copy, so several boards do not share one set of globals.
        handle, self._private = tempfile.mkstemp(prefix="hostsim-",
                                                 suffix=".so")
        os.close(handle)
        shutil.copyfile(path, self._private)

        self._lib = ctypes.CDLL(self._private)
        self._bind()
        self._lib.sim_init(ctypes.c_ulong(freq))
        self._buffer = ctypes.create_string_buffer(65536)
        self._started = False

    def _bind(self):
        lib = self._lib
        lib.sim_init.argtypes = [ctypes.c_ulong]
        lib.sim_init.restype = None
        lib.sim_start.argtypes = []
        lib.sim_start.restype = None
        lib.sim_step.argtypes = [ctypes.c_ulong]
        lib.sim_step.restype = ctypes.c_ulong
        lib.sim_finished.argtypes = []
        lib.sim_finished.restype = ctypes.c_int
        lib.sim_uart_read.argtypes = [ctypes.c_char_p, ctypes.c_ulong]
        lib.sim_uart_read.restype = ctypes.c_ulong
        lib.sim_uart_feed.argtypes = [ctypes.c_char_p, ctypes.c_ulong]
        lib.sim_uart_feed.restype = None
        for name in ("sim_ticks", "sim_now"):
            fn = getattr(lib, name)
            fn.argtypes = []
            fn.restype = ctypes.c_ulong
        lib.sim_mmu_on.argtypes = []
        lib.sim_mmu_on.restype = ctypes.c_int
        for name in ("sim_motor_duty", "sim_encoder_read", "sim_target"):
            fn = getattr(lib, name)
            fn.argtypes = []
            fn.restype = ctypes.c_long
        for name in ("sim_set_encoder", "sim_set_target"):
            fn = getattr(lib, name)
            fn.argtypes = [ctypes.c_long]
            fn.restype = None
        lib.sim_link_pop.argtypes = [ctypes.c_char_p, ctypes.c_ulong]
        lib.sim_link_pop.restype = ctypes.c_ulong
        lib.sim_link_push.argtypes = [ctypes.c_char_p, ctypes.c_ulong]
        lib.sim_link_push.restype = ctypes.c_int
        for name in ("sim_link_sent", "sim_link_received", "sim_link_dropped"):
            fn = getattr(lib, name)
            fn.argtypes = []
            fn.restype = ctypes.c_ulong
        for name in ("sim_fault_encoder_stuck", "sim_fault_link_down"):
            fn = getattr(lib, name)
            fn.argtypes = [ctypes.c_int]
            fn.restype = None
        lib.sim_fault_encoder_bias.argtypes = [ctypes.c_long]
        lib.sim_fault_encoder_bias.restype = None
        lib.sim_fault_link_drop_every.argtypes = [ctypes.c_ulong]
        lib.sim_fault_link_drop_every.restype = None
        lib.uart_rx_ready.argtypes = []
        lib.uart_rx_ready.restype = ctypes.c_int
        lib.sim_push_frame.argtypes = [ctypes.c_char_p, ctypes.c_ulong]
        lib.sim_push_frame.restype = None
        for name in ("sim_frames_pushed", "sim_frames_overwritten",
                     "sim_frame_size"):
            fn = getattr(lib, name)
            fn.argtypes = []
            fn.restype = ctypes.c_ulong
        lib.sim_accel_backend.argtypes = []
        lib.sim_accel_backend.restype = ctypes.c_char_p
        for name in ("sim_accel_available", "sim_accel_selftest"):
            fn = getattr(lib, name)
            fn.argtypes = []
            fn.restype = ctypes.c_int

    # -- running ----------------------------------------------------
    def start(self):
        """Begin the application, stopping when it first asks for time."""
        if not self._started:
            self._started = True
            self._lib.sim_start()
        return self

    def step(self, counter_ticks):
        """Advance virtual time. Returns timer interrupts taken."""
        if not self._started:
            self.start()
        return int(self._lib.sim_step(ctypes.c_ulong(int(counter_ticks))))

    def step_ms(self, milliseconds):
        """Advance virtual time by a number of milliseconds."""
        return self.step(self.freq * milliseconds // 1000)

    @property
    def finished(self):
        return bool(self._lib.sim_finished())

    # -- console ----------------------------------------------------
    def read(self):
        """Everything transmitted since the last call, as text."""
        n = self._lib.sim_uart_read(self._buffer, len(self._buffer))
        return self._buffer.raw[:n].decode("utf-8", errors="replace")

    def feed(self, text):
        """Push text into the console receive path, as a terminal would."""
        if isinstance(text, str):
            text = text.encode()
        self._lib.sim_uart_feed(text, len(text))

    # -- actuators and sensors --------------------------------------
    @property
    def motor_duty(self):
        """What the application last wrote to its motor output."""
        return int(self._lib.sim_motor_duty())

    @property
    def encoder(self):
        return int(self._lib.sim_encoder_read())

    @encoder.setter
    def encoder(self, position):
        self._lib.sim_set_encoder(ctypes.c_long(int(position)))

    @property
    def target(self):
        return int(self._lib.sim_target())

    @target.setter
    def target(self, value):
        self._lib.sim_set_target(ctypes.c_long(int(value)))

    # -- link -------------------------------------------------------
    def link_pop(self):
        """One message the board has sent, or None."""
        n = self._lib.sim_link_pop(self._buffer, len(self._buffer))
        return self._buffer.raw[:n] if n else None

    def link_pop_all(self):
        """Every message the board has sent since the last call."""
        out = []
        while True:
            message = self.link_pop()
            if message is None:
                return out
            out.append(message)

    def link_push(self, data):
        """Deliver a message to the board. False means its queue is full."""
        if isinstance(data, str):
            data = data.encode()
        return self._lib.sim_link_push(data, len(data)) == 0

    @property
    def link_stats(self):
        return {
            "sent": int(self._lib.sim_link_sent()),
            "received": int(self._lib.sim_link_received()),
            "dropped": int(self._lib.sim_link_dropped()),
        }

    # -- frames and the accelerator ---------------------------------
    def push_frame(self, data):
        """Hand the board one frame.

        There is one frame of slack, not a queue: a frame the board has not
        consumed by the time the next arrives is overwritten, as a camera
        DMAing into a double buffer would. `frames_overwritten` counts those.
        """
        if not isinstance(data, (bytes, bytearray)):
            data = bytes(bytearray(data))
        self._lib.sim_push_frame(bytes(data), len(data))

    @property
    def frame_size(self):
        return int(self._lib.sim_frame_size())

    @property
    def frames_pushed(self):
        return int(self._lib.sim_frames_pushed())

    @property
    def frames_overwritten(self):
        return int(self._lib.sim_frames_overwritten())

    @property
    def accel_backend(self):
        """"cpu", "cuda", and so on -- what the inference actually ran on."""
        return self._lib.sim_accel_backend().decode()

    @property
    def accel_available(self):
        """True when a real accelerator is behind the seam."""
        return bool(self._lib.sim_accel_available())

    def accel_selftest(self):
        """Disagreements between the accelerator and the C reference.

        0 is a pass. A CPU-only build also returns 0, because there is
        nothing to compare -- check `accel_available` to tell the two apart.
        """
        return int(self._lib.sim_accel_selftest())

    # -- injected faults --------------------------------------------
    def fault_encoder_stuck(self, on=True):
        """Freeze the encoder reading while the shaft keeps turning."""
        self._lib.sim_fault_encoder_stuck(1 if on else 0)

    def fault_encoder_bias(self, counts):
        """Offset every encoder reading by a fixed number of counts."""
        self._lib.sim_fault_encoder_bias(ctypes.c_long(int(counts)))

    def fault_link_down(self, on=True):
        """Silently discard everything the board sends."""
        self._lib.sim_fault_link_down(1 if on else 0)

    def fault_link_drop_every(self, n):
        """Discard one message in every `n`. 0 disables."""
        self._lib.sim_fault_link_drop_every(ctypes.c_ulong(int(n)))

    # -- observation ------------------------------------------------
    @property
    def ticks(self):
        return int(self._lib.sim_ticks())

    @property
    def now(self):
        """The architected counter, in ticks."""
        return int(self._lib.sim_now())

    @property
    def elapsed_ms(self):
        return self.now * 1000.0 / self.freq

    @property
    def mmu_on(self):
        return bool(self._lib.sim_mmu_on())

    def close(self):
        try:
            os.unlink(self._private)
        except OSError:
            pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    def __repr__(self):
        return "<Sim %s t=%.1fms ticks=%d>" % (
            self.name, self.elapsed_ms, self.ticks)


class Fleet:
    """Several boards advanced in lockstep.

    The boards share a virtual clock, so a run is reproducible whatever the
    host is doing. Wiring between them -- a UART cross-connect, a socket, a
    shared bus -- goes in a callback invoked after each step, which is the
    point at which every board has reached the same virtual time.
    """

    def __init__(self, sims=None, on_step=None, router=None, endpoints=None):
        self.sims = list(sims or [])
        #: Things that take part in routing but are not boards -- a socket
        #: bridge, a recorder, a fake peer. Anything with a `name`, a
        #: `link_push(message)` and a `link_pop_all()` will do; a `poll()` is
        #: called after each step if it has one.
        self.endpoints = list(endpoints or [])
        self.on_step = on_step
        #: Called as router(fleet, sender, message). Return an iterable of
        #: (recipient, message) pairs, or None to drop. Defaults to
        #: :meth:`broadcast`.
        self.router = router if router is not None else self.broadcast
        #: Messages that reached no recipient, for a test to assert on.
        self.undelivered = []

    @property
    def participants(self):
        """Boards and endpoints together, in routing order."""
        return self.sims + self.endpoints

    def add_endpoint(self, endpoint):
        self.endpoints.append(endpoint)
        return endpoint

    @staticmethod
    def broadcast(fleet, sender, message):
        """Deliver to every participant except the sender.

        Endpoints are included, so a socket bridge sees board traffic without
        any special casing -- which is the point of them looking like boards.
        """
        return [(p, message) for p in fleet.participants if p is not sender]

    def by_name(self, name):
        for participant in self.participants:
            if participant.name == name:
                return participant
        raise KeyError(name)

    def deliver(self):
        """Move messages between boards. Called after each step.

        Delivery happens once every board has reached the same virtual time,
        so a message sent during a step arrives at the start of the next one.
        That one-step latency is deliberate: it is roughly what a real link
        costs, and it stops the result depending on the order boards happen
        to be listed in.
        """
        # Endpoints are serviced first so anything that arrived from outside
        # during the step is routed in the same pass as board traffic.
        for endpoint in self.endpoints:
            poll = getattr(endpoint, "poll", None)
            if poll is not None:
                poll()

        in_flight = []
        for sender in self.participants:
            for message in sender.link_pop_all():
                in_flight.append((sender, message))

        for sender, message in in_flight:
            routed = self.router(self, sender, message)
            if not routed:
                self.undelivered.append((sender.name, message))
                continue
            for recipient, payload in routed:
                if not recipient.link_push(payload):
                    self.undelivered.append((recipient.name, payload))

        # Flush anything routed *to* an endpoint in this pass, so a message
        # leaves for the wire in the step that produced it rather than the
        # next one.
        for endpoint in self.endpoints:
            poll = getattr(endpoint, "poll", None)
            if poll is not None:
                poll()
        return len(in_flight)

    def add(self, sim):
        self.sims.append(sim)
        return sim

    def start(self):
        for sim in self.sims:
            sim.start()
        return self

    def step(self, counter_ticks):
        fired = [s.step(counter_ticks) for s in self.sims]
        self.deliver()
        if self.on_step is not None:
            self.on_step(self)
        return fired

    def step_ms(self, milliseconds):
        fired = [s.step_ms(milliseconds) for s in self.sims]
        self.deliver()
        if self.on_step is not None:
            self.on_step(self)
        return fired

    @property
    def finished(self):
        return all(s.finished for s in self.sims)

    def close(self):
        for sim in self.sims:
            sim.close()
        for endpoint in self.endpoints:
            close = getattr(endpoint, "close", None)
            if close is not None:
                close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

"""Windows audio layer: device + session discovery and WASAPI shared-mode capture.

Everything here is read-only except WasapiStream, which opens a shared-mode *capture*
stream (a mic, or loopback of an output). Nothing here plays audio or changes a device
setting. All functions must run on a COM-initialized worker thread (com_thread_init());
the Qt main thread never touches these objects.
"""

import ctypes
import os
import threading
import time
import warnings
from ctypes import HRESULT, POINTER, Structure, c_ubyte, c_uint32, c_ulonglong
from ctypes.wintypes import DWORD, WORD
from dataclasses import dataclass, field

import comtypes
import numpy as np
import psutil
from comtypes import COMMETHOD, GUID, IUnknown
from pycaw.api.audioclient import IAudioClient
from pycaw.api.audiopolicy import IAudioSessionControl2, IAudioSessionManager2
from pycaw.api.endpointvolume import IAudioMeterInformation
from pycaw.constants import DEVICE_STATE, EDataFlow
from pycaw.utils import AudioUtilities

OWN_PID = os.getpid()
TEAMS_EXE = "ms-teams.exe"

PK_FRIENDLY_NAME = "{A45C254E-DF1C-4EFD-8020-67D146A850E0} 14"
PK_DEVICE_DESC = "{A45C254E-DF1C-4EFD-8020-67D146A850E0} 2"
PK_ENUMERATOR = "{A45C254E-DF1C-4EFD-8020-67D146A850E0} 24"
PK_INTERFACE_NAME = "{026E516E-B814-414B-83CD-856D6FEF4822} 2"
SESSION_STATES = {0: "Inactive", 1: "Active", 2: "Expired"}
PRESENT_MASK = DEVICE_STATE.ACTIVE.value | DEVICE_STATE.DISABLED.value | DEVICE_STATE.UNPLUGGED.value


def com_thread_init() -> None:
    """Join the multithreaded apartment; call once at the start of every audio thread.

    A thread that already has COM (e.g. the main thread, initialised by importing comtypes)
    keeps its apartment; that's fine for code that stays on that thread.
    """
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError as e:
        if e.winerror != -2147417850:  # RPC_E_CHANGED_MODE
            raise


# --------------------------------------------------------------------------- endpoints

@dataclass
class Endpoint:
    id: str
    name: str
    flow: str  # "render" | "capture"
    active: bool
    is_bluetooth: bool
    is_call_mode: bool  # opening it would switch a Bluetooth headset into call (hands-free) mode
    _dev: object = field(repr=False, default=None)


_props_cache: dict[str, dict] = {}
_props_lock = threading.Lock()


def _endpoint(dev, dev_id: str, state: int, flow: str) -> Endpoint:
    with _props_lock:
        if dev_id not in _props_cache:
            _props_cache[dev_id] = AudioUtilities.CreateDevice(dev).properties
        p = _props_cache[dev_id]
    name = p.get(PK_FRIENDLY_NAME) or p.get(PK_DEVICE_DESC) or dev_id
    enumerator = str(p.get(PK_ENUMERATOR) or "").upper()
    blob = " ".join(str(v) for v in p.values() if isinstance(v, str)).upper()
    is_bt = "BTHENUM" in blob or "BTHHFENUM" in blob or "BTHLE" in blob or enumerator.startswith("BTH")
    # Every classic-Bluetooth mic is call-mode (HFP), plus legacy separate "Hands-Free" outputs.
    # Windows 11's ACP stack hides those behind the normal "Headphones (...)" output.
    label = f"{name} {p.get(PK_INTERFACE_NAME) or ''}".upper()
    call_mode = is_bt and (flow == "capture" or "HANDS-FREE" in label or enumerator == "BTHHFENUM")
    return Endpoint(dev_id, name, flow, state == DEVICE_STATE.ACTIVE.value, is_bt, call_mode, dev)


def list_endpoints(active_only: bool = True) -> list[Endpoint]:
    mask = DEVICE_STATE.ACTIVE.value if active_only else PRESENT_MASK
    enum = AudioUtilities.GetDeviceEnumerator()
    out = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # pycaw warns about unreadable properties on some endpoints
        for flow, value in (("render", EDataFlow.eRender.value), ("capture", EDataFlow.eCapture.value)):
            coll = enum.EnumAudioEndpoints(value, mask)
            for i in range(coll.GetCount()):
                try:
                    dev = coll.Item(i)
                    out.append(_endpoint(dev, dev.GetId(), dev.GetState(), flow))
                except comtypes.COMError:
                    continue  # device vanished mid-enumeration
    return out


def endpoint_peak(ep: Endpoint) -> float:
    """Current output peak (what the Sound control panel shows). Passive; use on outputs only."""
    try:
        meter = ep._dev.Activate(IAudioMeterInformation._iid_, comtypes.CLSCTX_ALL, None)
        return float(meter.QueryInterface(IAudioMeterInformation).GetPeakValue())
    except comtypes.COMError:
        return 0.0


# --------------------------------------------------------------------------- sessions

@dataclass
class Session:
    pid: int
    process: str
    state: str
    is_teams: bool
    peak: float


_proc_cache: dict[int, tuple[float, str, bool]] = {}


def _process_info(pid: int) -> tuple[str, bool]:
    """(process name, part of the ms-teams.exe process tree). Cached for 60 s per pid."""
    if pid == 0:
        return ("System Sounds", False)
    hit = _proc_cache.get(pid)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1], hit[2]
    try:
        proc = psutil.Process(pid)
        name = proc.name()
        is_teams = any(n.lower() == TEAMS_EXE for n in [name] + [p.name() for p in proc.parents()])
    except psutil.Error:
        name, is_teams = f"pid{pid}", False
    _proc_cache[pid] = (time.monotonic(), name, is_teams)
    return name, is_teams


def sessions(ep: Endpoint) -> list[Session]:
    """Audio sessions on an active endpoint (all processes except ours). Passive."""
    if not ep.active:
        return []
    try:
        mgr = ep._dev.Activate(IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None)
        enum = mgr.QueryInterface(IAudioSessionManager2).GetSessionEnumerator()
        out = []
        for i in range(enum.GetCount()):
            ctl = enum.GetSession(i)
            if ctl is None:
                continue
            ctl2 = ctl.QueryInterface(IAudioSessionControl2)
            pid = ctl2.GetProcessId()
            if pid == OWN_PID:
                continue
            try:
                level = float(ctl2.QueryInterface(IAudioMeterInformation).GetPeakValue())
            except comtypes.COMError:
                level = 0.0
            name, is_teams = _process_info(pid)
            out.append(Session(pid, name, SESSION_STATES.get(ctl2.GetState(), "?"), is_teams, level))
        return out
    except comtypes.COMError:
        return []


# --------------------------------------------------------------------------- capture

class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetBuffer",
                  (["out"], POINTER(POINTER(c_ubyte)), "ppData"),
                  (["out"], POINTER(c_uint32), "pNumFramesToRead"),
                  (["out"], POINTER(DWORD), "pdwFlags"),
                  (["out"], POINTER(c_ulonglong), "pu64DevicePosition"),
                  (["out"], POINTER(c_ulonglong), "pu64QPCPosition")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "NumFramesRead")),
        COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "pNumFramesInNextPacket")),
    )


class WAVEFORMATEXTENSIBLE(Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", WORD), ("nChannels", WORD), ("nSamplesPerSec", DWORD), ("nAvgBytesPerSec", DWORD),
        ("nBlockAlign", WORD), ("wBitsPerSample", WORD), ("cbSize", WORD),
        ("wValidBitsPerSample", WORD), ("dwChannelMask", DWORD), ("SubFormat", GUID),
    ]


AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
WAVE_FORMAT_IEEE_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
SUBTYPE_IEEE_FLOAT = GUID("{00000003-0000-0010-8000-00AA00389B71}")
HNS_PER_S = 10_000_000


class WasapiStream:
    """Shared-mode capture of one endpoint (loopback for outputs), mono float32 out.

    read() places audio on the QPC timeline (same clock as time.perf_counter): delivery
    gaps (loopback sends nothing while nothing plays) come back as zeros, so every track
    lines up with every other track. Any COM failure (device unplugged, Bluetooth profile
    switch) just ends the stream: `invalidated` becomes True and read() returns nothing.
    """

    def __init__(self, ep: Endpoint, buffer_s: float = 2.0):
        self.ep = ep
        client = ep._dev.Activate(IAudioClient._iid_, comtypes.CLSCTX_ALL, None).QueryInterface(IAudioClient)
        fmt_ptr = client.GetMixFormat()
        try:
            fmt = fmt_ptr.contents
            self.rate, self.channels, self.block_align = fmt.nSamplesPerSec, fmt.nChannels, fmt.nBlockAlign
            bits = fmt.wBitsPerSample
            is_float = fmt.wFormatTag == WAVE_FORMAT_IEEE_FLOAT
            if fmt.wFormatTag == WAVE_FORMAT_EXTENSIBLE:
                is_float = ctypes.cast(fmt_ptr, POINTER(WAVEFORMATEXTENSIBLE)).contents.SubFormat == SUBTYPE_IEEE_FLOAT
            if is_float and bits == 32:
                self._dtype, self._scale = np.float32, 1.0
            elif bits == 16:
                self._dtype, self._scale = np.int16, 1 / 32768.0
            elif bits == 32:
                self._dtype, self._scale = np.int32, 1 / 2147483648.0
            else:
                raise RuntimeError(f"unsupported mix format: {bits}-bit, tag {fmt.wFormatTag:#x}")
            flags = AUDCLNT_STREAMFLAGS_LOOPBACK if ep.flow == "render" else 0
            client.Initialize(AUDCLNT_SHAREMODE_SHARED, flags, int(buffer_s * HNS_PER_S), 0, fmt_ptr, None)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(fmt_ptr)
        self._client = client
        self._capture = client.GetService(IAudioCaptureClient._iid_).QueryInterface(IAudioCaptureClient)
        self.start_t = 0.0  # QPC seconds of sample 0 (re-anchored to the first packet)
        self.frames = 0  # frames placed on the timeline, including filled gaps
        self.gap_frames = 0
        self.invalidated = False
        self.error: str | None = None

    def start(self) -> None:
        self._client.Start()
        self.start_t = time.perf_counter()

    def stop(self) -> None:
        """Stop and fully release the client (Stop() alone keeps it initialized on the device)."""
        if self._client is not None:
            try:
                self._client.Stop()
            except comtypes.COMError:
                pass
        self._capture = None
        self._client = None
        self.invalidated = True

    def read(self) -> np.ndarray:
        """Drain all available packets. Never raises on device errors."""
        if self._capture is None:
            return np.zeros(0, np.float32)
        blocks = []
        try:
            while self._capture.GetNextPacketSize():
                data, n, flags, _devpos, qpc = self._capture.GetBuffer()
                t = qpc / HNS_PER_S if qpc else self.start_t + self.frames / self.rate
                if self.frames == 0:
                    self.start_t = t
                gap = int(round((t - (self.start_t + self.frames / self.rate)) * self.rate))
                if gap > self.rate * 0.03:  # >30 ms hole: fill with silence
                    blocks.append(np.zeros(gap, np.float32))
                    self.frames += gap
                    self.gap_frames += gap
                if flags & AUDCLNT_BUFFERFLAGS_SILENT or n == 0:
                    block = np.zeros(n, np.float32)
                else:
                    raw = ctypes.string_at(ctypes.cast(data, ctypes.c_void_p).value, n * self.block_align)
                    block = (np.frombuffer(raw, self._dtype).reshape(-1, self.channels).mean(axis=1)
                             * self._scale).astype(np.float32)
                self._capture.ReleaseBuffer(n)
                blocks.append(block)
                self.frames += n
        except comtypes.COMError as e:
            self.invalidated = True
            self.error = f"{e.hresult & 0xFFFFFFFF:#010x}"
        return np.concatenate(blocks) if blocks else np.zeros(0, np.float32)

    def pad_to(self, t: float) -> int:
        """Frames of silence needed to extend the track up to QPC time t (when closing)."""
        missing = int((t - self.start_t) * self.rate) - self.frames
        if missing > 0:
            self.frames += missing
            self.gap_frames += missing
        return max(0, missing)


# --------------------------------------------------------------------------- dsp helpers

def resample_to_16k(x: np.ndarray, rate: int) -> np.ndarray:
    if rate == 16000 or x.size == 0:
        return x.astype(np.float32)
    cutoff = 0.45 * 16000 / rate
    taps = np.arange(-64, 65)
    kernel = np.sinc(2 * cutoff * taps) * np.hamming(len(taps))
    y = np.convolve(x, (kernel / kernel.sum()).astype(np.float32), mode="same")
    if rate % 16000 == 0:
        return y[:: rate // 16000].astype(np.float32)
    n_out = int(len(y) * 16000 / rate)
    return np.interp(np.arange(n_out) * rate / 16000, np.arange(len(y)), y).astype(np.float32)


def dbfs(x: np.ndarray) -> float:
    if x.size == 0:
        return -120.0
    return float(20 * np.log10(max(np.sqrt(np.mean(x.astype(np.float64) ** 2)), 1e-6)))

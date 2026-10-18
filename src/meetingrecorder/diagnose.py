"""Read-only check of what MeetingRecorder can see:  python -m meetingrecorder.diagnose

Lists your audio devices, which apps are using them, whether a Teams call is detected right
now, then watches output levels for a few seconds. It never opens an audio stream, so it is
safe to run during a call. It also reports whether transcription will run on the GPU.
"""

import argparse
import time

from . import winaudio as wa


def _devices() -> list[wa.Endpoint]:
    eps = wa.list_endpoints()
    print("\nAudio devices (active):")
    for ep in eps:
        tags = [t for t, on in (("Bluetooth", ep.is_bluetooth), ("call-mode", ep.is_call_mode)) if on]
        print(f"  {'out' if ep.flow == 'render' else 'mic':<3}  {ep.name}" + (f"  [{', '.join(tags)}]" if tags else ""))
        for s in wa.sessions(ep):
            if s.pid == 0:
                continue
            who = "Teams" if s.is_teams else s.process
            print(f"         used by {who} ({s.process}, pid {s.pid}): {s.state}")
    return eps


def _call(eps: list[wa.Endpoint]) -> None:
    mics = [ep.name for ep in eps if ep.flow == "capture"
            and any(s.is_teams and s.state == "Active" for s in wa.sessions(ep))]
    outs = [ep.name for ep in eps if ep.flow == "render"
            and any(s.is_teams and s.state == "Active" for s in wa.sessions(ep))]
    print("\nTeams call:", f"YES, mic = {mics}, speaker = {outs or 'not playing yet'}" if mics else
          "no (a call is detected when Teams is actively using a microphone)")


def _levels(seconds: int, eps: list[wa.Endpoint]) -> None:
    outs = [ep for ep in eps if ep.flow == "render"]
    print(f"\nOutput levels for {seconds} s (play something to see it move):")
    for _ in range(seconds):
        parts = [f"{ep.name[:28]} {'#' * int(wa.endpoint_peak(ep) * 20):<20}" for ep in outs]
        print("  " + " | ".join(parts))
        time.sleep(1)


def _gpu() -> None:
    print("\nTranscription:")
    try:
        import ctranslate2

        n = ctranslate2.get_cuda_device_count()
        print(f"  CUDA GPUs visible to the speech model: {n}" + ("" if n else "  (will use the CPU: slower)"))
    except Exception as e:
        print(f"  could not check CUDA: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m meetingrecorder.diagnose", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=int, default=5, help="how long to watch output levels")
    args = ap.parse_args()
    wa.com_thread_init()
    eps = _devices()
    _call(eps)
    _levels(args.seconds, eps)
    _gpu()


if __name__ == "__main__":
    main()

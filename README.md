<p align="center">
  <img src="docs/icon.png" width="96" alt="MeetingRecorder icon">
</p>

<h1 align="center">Botless Meeting Transcriber</h1>

<p align="center">
  <b>Clean Teams transcripts without inviting a bot into your call.</b><br>
  Runs on your own Windows PC. Transcribes on your own GPU. Nothing leaves your machine.
</p>

<p align="center">
  <a href="LICENSE"><img alt="MIT licence" src="https://img.shields.io/badge/licence-MIT-green"></a>
  <img alt="Windows 11" src="https://img.shields.io/badge/platform-Windows%2011-0078D4">
  <img alt="Python 3.12" src="https://img.shields.io/badge/python-3.12-3776AB">
  <img alt="Local only" src="https://img.shields.io/badge/data-stays%20local-555">
</p>

---

Most AI note takers work by sending a bot into your meeting. Everyone sees it join, some organisations block it, and your audio goes off to someone else's servers.

This one doesn't do any of that.

MeetingRecorder sits in your system tray. When a Teams call starts it asks once if you want to record it. It then listens to the same two things you do, your microphone and what Teams plays to you, and after the call it turns them into a timestamped **Me / Others** transcript with Whisper running locally. You click **Copy transcript** and paste it into whatever AI you like to get your notes, action items and summaries.

<p align="center">
  <img src="docs/screenshot-recording.png" width="420" alt="MeetingRecorder recording a call, with live 'Hearing them' and 'Hearing you' meters">
  &nbsp;&nbsp;
  <img src="docs/screenshot-prompt.png" width="340" alt="The 'Teams call detected, record this call?' prompt">
</p>

## What it does

- **Spots Teams calls by itself** and shows a small "Record this call?" card. Nothing records without your click.
- **Records both sides, already labelled.** Your mic and the call audio go to separate tracks, so "Me" and "Others" come for free, no speaker-identification model needed.
- **Follows your devices.** Headphones connecting mid-call, Teams switching speaker, a Bluetooth headset flipping into call mode: it picks up the new device within a second or two and keeps going.
- **Stops when the call ends**, about 30 seconds after Teams lets go of the mic. Muting yourself doesn't count as the call ending.
- **Transcribes after the call, not during it.** It waits until no call is active before it starts, so it isn't fighting Teams for your GPU mid-meeting. On a mid-range laptop GPU a 1.5 minute call was transcribed about 10 seconds after recording stopped. No NVIDIA GPU? It falls back to the CPU, just slower.
- **Cleans up the transcript.** It drops speaker echo (when your mic hears the call on a speaker), filters Whisper's classic hallucinations like a stray "Thank you.", and only keeps audio from the device Teams was actually playing on.
- **Tells you if something is wrong while it still matters.** Live "Hearing them / Hearing you" meters, plus a red warning if Teams is playing but nothing is being captured.
- **Survives crashes.** If the app crashes mid-call you lose at most the last couple of seconds, and a power cut costs at most about ten. Anything left unfinished is transcribed on the next launch.
- **Tidies up after itself.** Audio is deleted once the transcript exists (you can keep it if you want). Transcripts are plain Markdown files.

## Built to be safe on a live call

I wanted something I could trust in a real meeting, so a few rules are baked in:

1. **It never opens a microphone on its own.** It only records a mic that another app, normally Teams, is already using, and lets go within about a second of that app releasing it. That matters with Bluetooth headphones: opening their mic is what forces them into low-quality "call mode", and this app can't cause that.
2. **It never plays sound and never changes your audio settings.** Capture is shared-mode only, so Teams and every other app carry on exactly as before.
3. **Nothing leaves your PC.** No account, no cloud, no telemetry. The only network call is a one-time download of the speech model from Hugging Face, pinned to an exact revision, and after that it runs offline. Copied transcripts are even kept out of Windows clipboard history and cloud clipboard sync.
4. **Heavy work waits.** Transcription doesn't start until no call is active.

More on why each of these exists in [docs/how-it-works.md](docs/how-it-works.md). Some of it took a few failed tests to find.

> **Please record responsibly.** No bot means no banner in the meeting, but recording people can still need their consent, your organisation's approval or both, depending on where you are. Check before you rely on it.

## Requirements

- Windows 11 and the **new Teams desktop app** (call detection is Teams-only; manual recording works with any app)
- [uv](https://docs.astral.sh/uv/) (installs Python 3.12 for you)
- An NVIDIA GPU is recommended, but not required
- About 1.6 GB of disk for the speech model, plus about 2 GB of NVIDIA libraries if you use the GPU. The model is [`mobiuslabsgmbh/faster-whisper-large-v3-turbo`](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo), downloaded on first use.

## Install

```powershell
git clone https://github.com/theSudoer/botless-meeting-transcriber.git
cd botless-meeting-transcriber
uv sync --extra cuda         # NVIDIA GPU. No NVIDIA GPU? Just: uv sync
uv run meetingrecorder --install
```

That creates a **MeetingRecorder** Start Menu shortcut. Open it, then right-click its taskbar icon and choose **Pin to taskbar**. "Start when I sign in to Windows" is in Settings.

## Use it

1. Leave it running. Closing the window keeps it in the tray, still listening for calls.
2. Join a Teams call and click **Record this call** on the card that pops up.
3. Hang up. It stops by itself, writes the transcript and shows a "Transcript ready" notification.
4. Select the meeting and click **Copy transcript**.

Transcripts land in `Documents\MeetingTranscripts\<date_time>\transcript.md` and look like this:

```markdown
# Meeting 15 Jan 2026, 10:00 (30 min)

_Recorded locally. Me = Headset (Bluetooth Headphones); Others = Headphones (Bluetooth Headphones)._

**[00:00:12] Others:** Morning everyone, shall we start with the release plan?

**[00:00:18] Me:** Sounds good. I can take the first item.
```

The second line names your audio devices, so it travels with anything you paste. Delete it first if you'd rather not share that.

Transcripts are saved in your local Documents folder. If you point the transcripts folder at a synced one (OneDrive, Dropbox), they will leave your PC through that sync.

**Want to test it without a meeting?** In Teams go to Settings, Devices, **Make a test call**. It gets recorded and transcribed like a real call.

**Want to see what the app sees?** This read-only check lists your audio devices, which apps are using them, whether a call is detected and whether a CUDA GPU is visible to the speech model. It's safe to run during a call:

```powershell
uv run python -m meetingrecorder.diagnose
```

## Good to know

- **Headphones give the best result.** On a speaker your mic also hears the call. Most of that echo is removed, but lines where you talk over the other person can still pick some up.
- **Don't play other audio on the same headphones during a meeting.** Sound on the device Teams uses ends up under "Others". During a Teams call, sound on a different device is filtered out.
- **Your mic is recorded while you're muted in Teams.** Teams keeps the mic open when muted, and so does this app.
- **English only for now.** The language is set to English for accuracy. Other languages are a small change in `transcriber.py`.

## How it works

```
 Teams call starts ──► scanner (1 s): which devices is Teams using?
                         │
        "Record this call?" ──► capture (10 ms): mic Teams uses + loopback of every output
                         │                       crash-safe WAV per device, one shared clock
 Teams releases mic ─────┘ (+30 s) ──► stop
                         │
                         ▼
 transcriber: cut each track into sound regions ─► Whisper (GPU) ─► keep Teams' device only
              ─► drop echo + hallucinations ─► merge into paragraphs ─► transcript.md
```

Three threads, and the UI never touches audio. The full story, including what failed along the way (Windows' per-app capture can't hear Teams calls, and running a voice detector over long silence quietly drops speech), is in **[docs/how-it-works.md](docs/how-it-works.md)**.

## Development

```powershell
uv run pytest              # fast unit tests
uv run pytest -m model     # tests that run the real speech model (GPU recommended)
uv run ruff check src tests
```

| Module | Job |
|---|---|
| `winaudio.py` | Windows audio: devices, per-app sessions, WASAPI capture on a shared QPC clock |
| `engine.py` | call detection, device following, capture, alarms, auto-stop, crash recovery |
| `transcriber.py` | region-based Whisper, Teams-device filter, echo and hallucination cleanup, Markdown |
| `wavio.py` | crash-safe WAV writer and tolerant reader |
| `app.py` | PySide6 window, tray, call prompt, transcripts list |
| `diagnose.py` | read-only device and call check |

Found a security issue? See [SECURITY.md](SECURITY.md).

## Licence

MIT. See [LICENSE](LICENSE).

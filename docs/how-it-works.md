# How it works, and what I learned building it

The idea is simple. A meeting bot hears two things: what you say and what everyone else says. Your own PC already has both. One is your microphone, the other is the audio Teams plays to you. Record those, transcribe them, done.

Getting it to work reliably on a real call took a bit more than that. These are the design decisions and the reasons behind them, most of which came from tests that didn't go the way I expected.

All observations below come from testing on one Windows 11 laptop with the new Teams desktop app, a pair of Bluetooth headphones, a Bluetooth speaker and the built-in laptop audio. Your hardware may behave differently. If it does, `python -m meetingrecorder.diagnose` shows what the app sees.

## 1. Two tracks instead of speaker identification

Your mic goes to one set of files and everything your PC plays goes to another. Anything on the mic track is "Me", anything on the output track is "Others".

That means no diarization model, no guessing who is who, and no GPU time spent on it. The trade-off is that everyone on the far side is one speaker, "Others". For meeting notes that turned out to be enough.

## 2. Bluetooth headphones are several devices in disguise

This was the first real problem. Classic Bluetooth headphones have two modes:

- **music mode** (A2DP): good stereo audio, no microphone
- **call mode** (HFP): a microphone, but low-quality mono audio for everything

The switch happens when any app opens the headphones' microphone. So if a recorder opens the mic "just in case", it drags your headphones into call mode and your audio gets worse. That's exactly the kind of thing that ruins a meeting.

So the app follows one rule: **it only opens a microphone that another app (Teams) is already actively using**, and it lets go within about a second of Teams releasing it. It finds that out by reading Windows' per-app audio sessions, which is a passive read. It can never be the reason your headset changed mode.

Two things I didn't expect:

- On my test machine, Windows 11's newer Bluetooth audio stack hides the old separate "Hands-Free" output devices. Teams plays the call to the normal "Headphones" device while the headset switches mode underneath. Recording that normal device is enough.
- Teams keeps its microphone session open while you're muted. Good news for detection: "Teams released the mic" really does mean the call ended, not that you muted yourself. It also means your mic is still recorded while muted, which the README calls out.

## 3. Recording the other side: what didn't work

Windows 10 and 11 have a per-app capture mode ("process loopback") that records only what one app plays. It sounded perfect: record only Teams, ignore YouTube in another tab.

In testing it captured a browser fine, but it was **completely silent for every Teams call**, even while Teams was clearly playing audio. Teams' call audio doesn't go through that path.

So the app records every output device instead (a passive "loopback" that changes nothing). It also logs which device Teams was playing on, second by second. At transcription time, "Others" only keeps audio from the device Teams was using at that moment. A video on your laptop speakers during a headphone call gets dropped. A video on the same headphones can't be separated, which the README mentions.

As a safety net, if that filter would remove every "Others" line, the transcript keeps them all and says so. Losing the other side of a meeting would be a much worse failure than a stray line from a video.

## 4. One clock for everything

Each device delivers audio on its own schedule. Loopback in particular sends nothing at all while nothing is playing. If you just appended packets to a file, your tracks would drift apart and the transcript order would be wrong.

Every WASAPI packet carries a QueryPerformanceCounter timestamp, the same clock as Python's `time.perf_counter()`. The app places each packet at its true position and fills gaps with silence. Every track, from every device, lines up with every other one.

## 5. The voice detector that quietly dropped speech

Whisper ships with a voice activity detector (Silero VAD) to skip silence. In a test call the mic track had about 74 seconds of digital silence before I spoke. The detector skipped my speech too, and the transcript for "Me" came back **empty**, no error, nothing.

The fix was to stop running one long, mostly-silent file through it. Each track is now cut into regions where there is sound clearly above that track's own noise floor, and each region is transcribed on its own. Short replies like "yes" get a second chance without the detector, but only if Whisper is confident. There are regression tests for exactly this: three short replies spread across four minutes of silence must all come back at the right times.

## 6. Cleaning up what Whisper gives you

- **Echo.** On a speaker, your mic hears the call. If a "Me" line closely matches an "Others" line within a couple of seconds either side, it's dropped.
- **Hallucinations.** Whisper likes to invent "Thank you." or "You" in near-silence. A line that is just "You" is always dropped. "Thank you." and a few similar phrases are dropped only when the model's own confidence is low, so a real "Thank you." from a person still survives.
- **Paragraphs.** Consecutive lines from the same side are merged, so the transcript reads like a conversation, not a log.

## 7. Never lose a recording

- WAV headers are rewritten every 2 seconds, so a file is valid up to the last couple of seconds if the app crashes. They're also forced to disk (`fsync`) every 10 seconds, which covers a power cut. Not more often: this runs on the capture thread, and a slow disk must never hold up the audio.
- `session.json` (devices, timings, which output Teams used) is rewritten every few seconds too, atomically: written to a temp file, flushed, then swapped in.
- `session.json` is only trusted so far: track files must sit inside that session's own `audio` folder, and only the WAVs a session recorded are ever deleted.
- On the next launch, any session still marked "recording", or one that stopped but never got a transcript (say you quit right after a call), is transcribed automatically.
- Microphones fail closed. If the device scan errors, or stalls for more than 3 seconds, the app releases any mic it holds, because it can no longer confirm another app is using it.
- Audio is only deleted after a transcript with content has been written. If transcription fails or finds nothing, the audio stays so you can retry.

## 8. Threads, and keeping the call smooth

| Thread | Every | Does |
|---|---|---|
| scanner | 1 s | lists devices and sessions, detects calls, decides what to capture, alarms, auto-stop |
| capture | 10 ms | opens and closes streams, drains audio into the crash-safe WAVs |
| transcriber | per job | waits until no call is active, then transcribes |
| UI (Qt) | 250 ms | reads a status snapshot; never touches audio objects |

The scanner and capture threads are separate for a reason. The very first process scan on Windows can take several seconds, and if that ever blocked the capture loop, audio buffers would overflow and you'd lose part of the call.

## 9. Privacy model

- Audio, transcripts and settings stay on your PC (`Documents\MeetingTranscripts`, `%APPDATA%`, `%LOCALAPPDATA%`).
- No telemetry, no accounts. The only network access is the one-time model download. After that the model is loaded from the local cache with no network calls.
- The app reads which apps use which audio devices (process names and IDs) to detect Teams, and, for a manual recording outside Teams, to find the mic another app already has open. That stays in memory. A session's `session.json` records audio device names and timings, not what other apps you had open.

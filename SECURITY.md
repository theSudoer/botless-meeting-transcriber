# Security policy

## Reporting a vulnerability

Please don't open a public issue for security problems. Use GitHub's private reporting instead: the **Security** tab of this repository, then **Report a vulnerability**.

Include what you found, how to reproduce it and what an attacker could do with it. I'll acknowledge it as soon as I can and keep you updated while it's fixed.

If you attach logs, please redact them first: they contain file paths, which include your Windows username.

## Scope and design

MeetingRecorder records meeting audio, so the things that matter most are where that audio goes and what the app can touch.

- **Local only.** No server, no account, no telemetry. The only network access is downloading the speech model from Hugging Face on first use, pinned to a fixed revision.
- **Least access to audio.** Shared-mode capture only. It never plays audio, never changes device settings, and only opens a microphone that another app (normally Teams) is already using. If it can't confirm that, it releases the mic.
- **No elevated rights.** It runs as your normal user and needs no admin access.

Everything it reads or writes:

| What | Where |
|---|---|
| Recordings (deleted after transcription by default) and transcripts | `Documents\MeetingTranscripts` (configurable) |
| Settings | `%APPDATA%\MeetingRecorder` |
| Logs: device names, file paths, timings and errors, never audio or transcript text | `%LOCALAPPDATA%\MeetingRecorder\logs` |
| App icon | `%LOCALAPPDATA%\MeetingRecorder` |
| Shortcuts it creates or updates: Start Menu, Startup (if enabled), your taskbar pin | `%APPDATA%\Microsoft\Windows\Start Menu\Programs`, `...\Internet Explorer\Quick Launch\User Pinned\TaskBar` |
| Speech model cache | `%USERPROFILE%\.cache\huggingface` |

Things that would count as a vulnerability:
- the app sending audio or transcripts anywhere;
- reading or writing outside the places above;
- opening a microphone that no other app is using;
- anything that lets another process or user make it do so.

## Supported versions

Fixes go to the latest commit on `main`.

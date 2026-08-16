#!/usr/bin/env python3
"""
transcribe_audio.py - LOCAL re-transcription helper for a family archive.

A thin faster-whisper wrapper. Family recordings don't announce their
speakers, so segments are left unlabeled for a human (or the
import-recordings label-transfer script) to attribute.

Runs on the machine that holds the audio. CPU works; any NVIDIA GPU is much
faster.

What it does:
  1. Takes a local audio/video file (a phone-recorded interview, an archived
     .m4a under the documents root, anything PyAV can decode).
  2. Extracts mono 16kHz audio with ffmpeg if available (PyAV fallback if not).
  3. Transcribes with faster-whisper.
  4. Writes <name>.txt (plain), <name>.srt (subtitles), <name>.md (timestamped,
     readable) into --outdir - all three at once, only after the whole
     recording has been transcribed (see publish_transcripts).

Two rules this file exists to keep:

  * ALL OR NOTHING. Whisper decodes lazily, so a bad frame, a model failure or
    a Ctrl-C lands in the middle of the segment loop. Every byte is written to
    a `.part` sibling and renamed into place only once the last segment is in,
    so an interrupted run leaves no half-transcript behind. This matters more
    than it sounds: the recommended way to work a long queue is to skip any
    recording whose output already exists, and a truncated file that looks
    finished would be skipped forever.

  * NOTHING MACHINE-SPECIFIC IN THE OUTPUT. The .md is written to be attached
    to a source record and kept forever, so it names the recording by FILENAME
    only. An absolute path would leak the operator's home directory into the
    archive and would be a lie the day the archive moves to another machine
    (AGENTS_TOOLING.md privacy rule; SPEC 12.4 stores paths in alias form).

The outputs are WORKING FILES until attached: review, then ALWAYS attach the
whisper pass to the recording's existing source with
    fha process <archived-audio> --more <documents-path>/<name>.md whisper-transcript
so the source record ends up carrying the audio, the original transcript
(role `transcript`), and the whisper pass (role `whisper-transcript`) side by
side. Never overwrite or detach the original transcript - the two transcripts
disagree in exactly the spots a reviewer needs to compare.

`fha process --more` renames what it attaches to `{stem}-{role}_{S-id}.{ext}`
and only attaches files that already live under the documents root - so pass
the source's shared stem to `--name` WITHOUT any role suffix of your own, and
copy the finished .md under the documents root before attaching it.

Setup (once):   pip install faster-whisper        # ffmpeg optional
Usage:
  python transcribe_audio.py FILE [--model small] [--outdir DIR] [--name BASE]
                                  [--language en] [--force]

Exit codes:  0 transcript written (or already present - nothing to do)
             1 ran, but the recording held no speech; nothing was written
             2 could not run: bad input, missing dependency, decode failure

Model guide (CPU, rough): base ~ 2-4x realtime, rough quality; small ~ 1-2x,
good default; medium ~ realtime, noticeably better with mumbled/overlapping
family conversation; large-v3 best but slow without a GPU. When the goal is
recovering garbled proper names, prefer medium or better.

CODE MAP
--------
  fmt_ts / srt_ts        - seconds -> display and SRT timestamps
  output_paths           - the three final files for one --outdir/--name
  default_output_name    - a sane --name from an already-archived filename
  name_problem           - plain-language validation of --name
  md_header              - the .md preamble (portable: filename, never a path)
  publish_transcripts    - the all-or-nothing writer (temp siblings + rename)
  prepare_audio          - ffmpeg extraction with a PyAV fallback
  build_parser           - the CLI surface (kept apart so a test can read it)
  main                   - CLI: check, transcribe, publish, say what is next
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

EXIT_OK = 0
EXIT_NO_SPEECH = 1
EXIT_FAILED = 2

# Crockford Base32 body of a source id, as it appears in a processed filename
# (`..._S-x9qcves0nm.m4a`). Kept local rather than imported from tools/_lib:
# a skill script must run standalone, from any folder, with no archive present.
_SOURCE_ID_SUFFIX = re.compile(r'_S-[0-9abcdefghjkmnpqrstvwxyz]{10}$', re.IGNORECASE)

# Role words a recording's own file commonly ends with. Stripped from a default
# --name because `fha process --more` appends the role itself; leaving one in
# produces `...-audio-whisper-transcript_S-...`, which sorts away from its
# siblings. An explicit --name always wins over this guess.
_MEDIA_ROLE_SUFFIXES = ('-audio', '-video', '-recording')


def fmt_ts(sec):
    """Seconds -> HH:MM:SS, the form quoted in claim notes ("whisper 00:41:22")."""
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def srt_ts(sec):
    """Seconds -> SRT's HH:MM:SS,mmm (comma before milliseconds, not a dot)."""
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    ms = int((sec - int(sec)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def output_paths(outdir, name):
    """The three files one run produces, in write order (txt, srt, md).

    One place computes these so the pre-flight "already transcribed?" check and
    the writer can never disagree about which files a run owns.
    """
    outdir = Path(outdir)
    return (outdir / f'{name}.txt', outdir / f'{name}.srt', outdir / f'{name}.md')


def default_output_name(stem):
    """Guess the shared source stem from the recording's own filename.

    An already-archived recording is named `{stem}-{role}_{S-id}.{ext}`. Both
    of those trailing parts have to go: `fha process --more` refuses a file
    that already carries an S-id, and it appends the role itself. So
    `hartley-thomas-1998-06-14-farm-audio_S-x9qcves0nm` defaults to
    `hartley-thomas-1998-06-14-farm`, which attaches back as
    `hartley-thomas-1998-06-14-farm-whisper-transcript_S-x9qcves0nm.md` and
    sorts beside the audio it came from.
    """
    stem = _SOURCE_ID_SUFFIX.sub('', stem)
    lowered = stem.lower()
    for role in _MEDIA_ROLE_SUFFIXES:
        if lowered.endswith(role) and len(stem) > len(role):
            return stem[: -len(role)]
    return stem


def name_problem(name):
    """Return a plain-language complaint about --name, or None if it is usable.

    --name becomes three filenames, so a path separator in it would scatter the
    outputs somewhere the caller did not ask for; an S-id in it would produce a
    file `fha process --more` then refuses to attach. Both are worth catching
    before an hour of transcription, not after.
    """
    if not name or not name.strip():
        return ("the output name is empty. Pass the recording's shared filename stem, "
                "e.g. --name hartley-thomas-1998-06-14-farm")
    if '/' in name or '\\' in name or os.sep in name:
        return (f"--name has to be a filename, not a path (got \"{name}\"). Pass just the stem, "
                "e.g. --name hartley-thomas-1998-06-14-farm, and put the folder in --outdir")
    if _SOURCE_ID_SUFFIX.search(name):
        stripped = _SOURCE_ID_SUFFIX.sub('', name)
        return (f"--name still carries the source id from the archived filename (\"{name}\"). "
                "`fha process --more` adds the id itself and refuses a file that already has "
                f"one - pass --name {stripped} instead")
    return None


def md_header(name, recording, model):
    """The .md preamble.

    `recording` is a BARE FILENAME on purpose. This file gets attached to a
    source record and kept for good, so an absolute path here would publish the
    operator's directory layout into the archive and would point nowhere the
    first time the archive is copied to another machine. The filename of a
    processed asset already carries its `_S-id`, which is the portable identity
    anyway.
    """
    return (
        f"# Whisper transcript: {name}\n\n"
        f"Recording: {recording}\n"
        f"Model: faster-whisper {model}\n\n"
        "Segments are unlabeled - speaker attribution is a human's (or a "
        "review pass's) job, matched against the recording. Timestamps "
        "reference the original audio file.\n\n"
    )


def publish_transcripts(outdir, name, segments, model, recording, progress=None):
    """Write the three transcripts all-or-nothing. Returns the segment count.

    Whisper hands back a LAZY iterator: the audio is decoded as the loop pulls
    from it. Writing straight to `<name>.txt/.srt/.md` therefore truncates all
    three at whatever second a decode error, an out-of-memory model or a Ctrl-C
    happens to land on - and leaves them looking finished. The skill recommends
    working long queues by skipping any recording whose outputs already exist,
    so such a stump would be treated as a completed pass and never retried.

    So: every byte goes to a `.part` sibling created in the SAME directory (same
    filesystem, which is what makes os.replace an atomic rename), and the final
    names appear only after the iterator has run dry. On any failure the temps
    are removed and no output file exists at all. The three replaces happen back
    to back at the very end; they are not one transaction, but the only window
    left is microseconds wide and cannot be opened by the transcription itself.

    A run that finds NO speech publishes nothing, for the same reason: an empty
    transcript is indistinguishable from a finished one to the next batch pass.
    The caller reports that (exit code 1) rather than leaving a stump on disk.

    `progress` is an optional callable taking the segment start time, so the
    console ticker stays out of the writing logic (and out of the tests).
    """
    outdir = Path(outdir)
    finals = output_paths(outdir, name)
    temps = []
    handles = []
    count = 0
    try:
        for final in finals:
            fd, tmp = tempfile.mkstemp(dir=str(outdir), prefix=final.name + '.', suffix='.part')
            temps.append(Path(tmp))
            handles.append(open(fd, 'w', encoding='utf-8', newline='\n'))
        ftxt, fsrt, fmd = handles
        fmd.write(md_header(name, recording, model))
        for i, seg in enumerate(segments, 1):
            text = (seg.text or '').strip()
            count += 1
            ftxt.write(text + "\n")
            fsrt.write(f"{i}\n{srt_ts(seg.start)} --> {srt_ts(seg.end)}\n{text}\n\n")
            fmd.write(f"**[{fmt_ts(seg.start)}]** {text}\n\n")
            if progress is not None:
                progress(seg.start)
        for handle in handles:
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
        handles = []
        if count == 0:
            return 0
        for tmp, final in zip(temps, finals):
            os.replace(tmp, final)
        temps = []
        return count
    finally:
        # Cleanup only, and quietly: if we got here on an exception, that
        # exception is the news. A close() or unlink() that fails while tidying
        # up must not replace it with a confusing one of its own.
        for handle in handles:
            try:
                handle.close()
            except OSError:
                pass
        for tmp in temps:
            try:
                tmp.unlink()
            except OSError:
                pass


def prepare_audio(src, workdir):
    """Return the path faster-whisper should read: a 16kHz mono wav, or the original.

    ffmpeg is the fast, forgiving decoder for phone containers, but it is an
    extra install a family archivist may not have. When it is missing or fails,
    faster-whisper's bundled PyAV reads the original file directly - slower and
    fussier with odd containers, but it keeps the skill usable with one pip
    install and no system packages.
    """
    wav = Path(workdir) / 'audio.wav'
    try:
        print("extracting audio (ffmpeg)...")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                        "-vn", "-ac", "1", "-ar", "16000", str(wav)], check=True)
        return wav
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print(f"  ffmpeg unavailable ({e.__class__.__name__}); decoding with PyAV instead")
        return Path(src)


def build_parser():
    """The CLI surface, built apart from main() so a test can check the flags.

    The skill's SKILL.md documents these by name; a flag that drifts out of one
    side or the other is exactly the kind of quiet contract break that only
    shows up when a human follows the written instructions at 11pm.
    """
    ap = argparse.ArgumentParser(
        description="Re-transcribe a family recording with faster-whisper")
    ap.add_argument("file", help="local audio/video file")
    ap.add_argument("--model", default="small",
                    help="faster-whisper model: tiny/base/small/medium/large-v3 (default small)")
    ap.add_argument("--outdir", default=None,
                    help="output directory (default: <file's folder>/whisper). Use a scratch "
                         "folder, not an asset root: three files are written under --name")
    ap.add_argument("--name", default=None,
                    help="output basename - the source's shared stem, with NO role suffix "
                         "(fha process --more appends the role). Default: from the filename")
    ap.add_argument("--language", default="en",
                    help="spoken language code, or 'auto' to let whisper detect it (default en)")
    ap.add_argument("--force", action="store_true",
                    help="re-transcribe even if this run's output files already exist")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)

    src = Path(a.file)
    if not src.is_file():
        print(f"no such file: {a.file}\n"
              "Check the path (quote it if it has spaces) and run the command again.",
              file=sys.stderr)
        return EXIT_FAILED

    name = a.name or default_output_name(src.stem)
    problem = name_problem(name)
    if problem is not None:
        print(f"{problem}", file=sys.stderr)
        return EXIT_FAILED

    outdir = Path(a.outdir) if a.outdir else src.resolve().parent / 'whisper'
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"could not create the output folder {outdir}: {e}\n"
              "Pick a folder you can write to with --outdir and run the command again.",
              file=sys.stderr)
        return EXIT_FAILED

    finals = output_paths(outdir, name)
    existing = [p for p in finals if p.exists()]
    if existing and not a.force:
        # Deliberately a success: this is what lets a long queue be re-run after
        # a reboot without re-doing hours of finished work. Nothing published is
        # ever partial (see publish_transcripts), so "it is there" means "it is
        # done" - and the message still names the way to redo it on purpose.
        print(f"already transcribed - these files are in {outdir}:")
        for p in existing:
            print(f"  {p.name}")
        print("Nothing was overwritten. Re-run with --force to replace them, "
              "or pass a different --name.")
        return EXIT_OK

    # Checked before any decoding: a missing engine should cost a second, not
    # the ffmpeg pass over a two-hour recording.
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("faster-whisper is not installed on this machine.\n"
              "Install it once with:  pip install faster-whisper\n"
              "then run this command again.", file=sys.stderr)
        return EXIT_FAILED

    with tempfile.TemporaryDirectory() as td:
        audio = prepare_audio(src, td)

        print(f"transcribing with faster-whisper '{a.model}' (this is the long part)...")

        def tick(start):
            sys.stdout.write(f"\r  {fmt_ts(start)} transcribed")
            sys.stdout.flush()

        language = None if a.language.strip().lower() in ('', 'auto') else a.language
        try:
            model = WhisperModel(a.model, compute_type="auto")
            segments, _info = model.transcribe(str(audio), language=language, vad_filter=True)
            count = publish_transcripts(outdir, name, segments, a.model, src.name,
                                        progress=tick)
        except KeyboardInterrupt:
            print("\nstopped before the transcript was finished. Nothing was written, so "
                  "re-running the same command starts this recording over cleanly.",
                  file=sys.stderr)
            return EXIT_FAILED
        except Exception as e:
            # No traceback for the human: name the cause and the next move.
            print(f"\ntranscription failed: {e}", file=sys.stderr)
            print("Nothing was written - no half-finished transcript was left behind.\n"
                  "Try: play the recording to confirm it is not corrupt, then run the same "
                  "command with --model small; if that works, the larger model ran out of "
                  "memory.", file=sys.stderr)
            return EXIT_FAILED

    if count == 0:
        print("\nno speech was found, so nothing was written.")
        print("Play the recording to confirm it has real audio; if it does, run the same "
              "command again with --language auto (a non-English recording is the usual "
              "cause).")
        return EXIT_NO_SPEECH

    md_path = finals[2]
    print(f"\ndone ({count} segments) in {outdir}:")
    for p in finals:
        print(f"  {p.name}")
    print("Review it, then copy the .md under the documents root beside the recording "
          "and attach it:\n"
          f'  fha process "<archived-audio>" --more "<documents-path>/{md_path.name}" '
          "whisper-transcript")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
transcribe_audio.py — LOCAL re-transcription helper for a family archive.

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
     readable) into --outdir.

The outputs are WORKING FILES until attached: review, then ALWAYS attach the
whisper pass to the recording's existing source with
    fha process <archived-audio> --more <name>.md whisper-transcript
so the source record ends up carrying the audio, the original transcript
(role `transcript`), and the whisper pass (role `whisper-transcript`) side by
side. Never overwrite or detach the original transcript — the two transcripts
disagree in exactly the spots a reviewer needs to compare.

Setup (once):   pip install faster-whisper        # ffmpeg optional
Usage:
  python transcribe_audio.py FILE [--model small] [--outdir DIR] [--name BASE]
                                  [--language en]

Model guide (CPU, rough): base ≈ 2-4x realtime, rough quality; small ≈ 1-2x,
good default; medium ≈ realtime, noticeably better with mumbled/overlapping
family conversation; large-v3 best but slow without a GPU. When the goal is
recovering garbled proper names, prefer medium or better.
"""
import argparse
import os
import subprocess
import sys
import tempfile


def fmt_ts(sec):
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def srt_ts(sec):
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    ms = int((sec - int(sec)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main():
    ap = argparse.ArgumentParser(description="Re-transcribe a family recording with faster-whisper")
    ap.add_argument("file", help="local audio/video file")
    ap.add_argument("--model", default="small",
                    help="faster-whisper model: tiny/base/small/medium/large-v3 (default small)")
    ap.add_argument("--outdir", default=None,
                    help="output directory (default: <file's folder>/whisper)")
    ap.add_argument("--name", default=None, help="output basename (default: from filename)")
    ap.add_argument("--language", default="en")
    a = ap.parse_args()

    if not os.path.isfile(a.file):
        sys.exit(f"no such file: {a.file}")
    name = a.name or os.path.splitext(os.path.basename(a.file))[0]
    outdir = a.outdir or os.path.join(os.path.dirname(os.path.abspath(a.file)), "whisper")
    os.makedirs(outdir, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        wav = os.path.join(td, "audio.wav")
        audio = wav
        try:
            print("extracting audio (ffmpeg)...")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", a.file,
                            "-vn", "-ac", "1", "-ar", "16000", wav], check=True)
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            print(f"  ffmpeg unavailable ({e.__class__.__name__}); decoding with PyAV instead")
            audio = a.file

        print(f"transcribing with faster-whisper '{a.model}' (this is the long part)...")
        from faster_whisper import WhisperModel
        model = WhisperModel(a.model, compute_type="auto")
        segments, info = model.transcribe(audio, language=a.language, vad_filter=True)

        txt_p = os.path.join(outdir, name + ".txt")
        srt_p = os.path.join(outdir, name + ".srt")
        md_p = os.path.join(outdir, name + ".md")
        seg_count = 0
        with open(txt_p, "w", encoding="utf-8") as ftxt, \
             open(srt_p, "w", encoding="utf-8") as fsrt, \
             open(md_p, "w", encoding="utf-8") as fmd:
            fmd.write(f"# Whisper transcript: {name}\n\nSource: {a.file}\nModel: {a.model}\n\n"
                      "Segments are unlabeled - speaker attribution is a human's (or a "
                      "review pass's) job, matched against the recording. Timestamps "
                      "reference the original audio file.\n\n")
            for i, seg in enumerate(segments, 1):
                text = seg.text.strip()
                seg_count += 1
                ftxt.write(text + "\n")
                fsrt.write(f"{i}\n{srt_ts(seg.start)} --> {srt_ts(seg.end)}\n{text}\n\n")
                fmd.write(f"**[{fmt_ts(seg.start)}]** {text}\n\n")
                sys.stdout.write(f"\r  {fmt_ts(seg.start)} transcribed")
                sys.stdout.flush()
        print(f"\ndone ({seg_count} segments):\n  {txt_p}\n  {srt_p}\n  {md_p}")
        if seg_count == 0:
            print("WARNING: zero speech segments detected - check the recording has "
                  "real audio before assuming a transcription failure.")


if __name__ == "__main__":
    main()

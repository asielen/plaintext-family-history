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

Three rules this file exists to keep:

  * ALL OR NOTHING. Whisper decodes lazily, so a bad frame, a model failure or
    a Ctrl-C lands in the middle of the segment loop. Every byte is written to
    a `.part` sibling and renamed into place only once the last segment is in,
    so an interrupted run leaves no half-transcript behind. This matters more
    than it sounds: the recommended way to work a long queue is to skip any
    recording whose output already exists, and a truncated file that looks
    finished would be skipped forever.

    Renaming three files is not one operation, though - `os.replace` is atomic
    for one file and there is no atomic multi-file rename on any platform this
    runs on. So the three renames are wrapped in a small commit protocol: every
    destination is checked before the first rename, each previous file is moved
    aside where it can be moved back, and a marker file says a promotion is in
    flight. A failure rolls the folder back to how it was; a hard kill (power
    loss, SIGKILL) that no rollback can catch leaves the marker behind, and the
    next run reads it, refuses to call the recording done, and redoes it. The
    one thing that must never happen - a mixed set of files that looks finished
    and is therefore skipped forever - cannot survive a single retry.

  * WHAT THIS PROGRAM DID NOT WRITE, IT DOES NOT REPLACE. --outdir is a folder
    the human picked, so a name this run wants can already be taken by a file
    of his - a `family.md` he typed himself, sitting where this run would put
    `family.md`. The marker above is also the ownership evidence: an unfinished
    promotion of ours ALWAYS leaves one. So a marked folder is redone without
    being asked, and an UNMARKED set of one or two files - which is just some
    files that happen to share a name - stops the run and asks for --force.
    Overwriting on a guess is what AGENTS.md forbids; the refusal names the
    exact command that would go ahead.

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
             2 could not run: bad input, missing dependency, decode failure, the
               transcript could not be saved where it was asked for, or the
               output folder holds files under these names that this program
               cannot show it wrote (see --force)

Model guide (CPU, rough): base ~ 2-4x realtime, rough quality; small ~ 1-2x,
good default; medium ~ realtime, noticeably better with mumbled/overlapping
family conversation; large-v3 best but slow without a GPU. When the goal is
recovering garbled proper names, prefer medium or better.

CODE MAP
--------
  fmt_ts / srt_ts        - seconds -> display and SRT timestamps
  output_paths           - the three final files for one --outdir/--name
  marker_path            - where the "promotion in flight" marker lives
  publication_state      - what is on disk: complete / partial / interrupted / none
  default_output_name    - a sane --name from an already-archived filename
  name_problem           - plain-language validation of --name
  md_header              - the .md preamble (portable: filename, never a path)
  PublishError           - "the transcript could not be saved", with the fix
  _destination_problem   - why one final name cannot be replaced, in plain words
  _preflight_destinations- refuse the run before the first rename, not after it
  _set_aside/_restore_previous/_promote_all - the three-file commit protocol
  publish_transcripts    - the all-or-nothing writer (temp siblings + promotion)
  prepare_audio          - ffmpeg extraction with a PyAV fallback
  build_parser           - the CLI surface (kept apart so a test can read it)
  rerun_command          - this run, plus a flag, as one copyable command line
  _leftover_note         - what a failed run actually left on disk
  main                   - CLI: check, transcribe, publish, say what is next
"""
import argparse
import os
import re
import shlex
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


# Written just before the three renames and removed once they are all done. Its
# text is addressed to whoever finds it in the folder, because that is usually
# a human wondering what the odd file is - not a program.
_MARKER_TEXT = (
    "A transcription run was moving its three transcript files into place here and did\n"
    "not finish. While this file exists, the transcripts beside it may be a mix of two\n"
    "different runs, so the next run redoes this recording instead of skipping it.\n"
    "Files ending .part are unfinished; files ending .kept hold the previous version.\n"
    "Once the transcript here looks right, this file and those leftovers can go.\n"
    "Files this run was writing:\n"
)


def marker_path(outdir, name):
    """Where one run's "promotion in flight" marker lives.

    Hidden (leading dot) and named for the run, so two recordings transcribed
    into one scratch folder never read each other's marker.
    """
    return Path(outdir) / f'.{name}.publishing'


def publication_state(outdir, name):
    """What this run's outputs look like on disk right now. The "is it done?" test.

    One of four words:
      'complete'    - all three files are there and no promotion was left open
      'interrupted' - the marker is still there, so a promotion was cut short and
                      the three files may be a mix of two runs
      'partial'     - only some of the three are there, and no marker says why
      'none'        - none of them are there

    Only 'complete' may be read as "already transcribed, skip it". That is the
    whole point of this function: the skill tells the human to work a long queue
    by skipping recordings whose outputs exist, so a test that any single file
    satisfies makes a half-published set permanent.

    The two answers carry different weight, and main treats them differently.
    'interrupted' is OUR unfinished work with our own evidence beside it, so it
    is redone unasked. 'partial' is weaker than it looks: this program cannot
    produce it by being interrupted (a run killed while writing leaves only
    `.part` siblings, and one killed mid-promotion leaves the marker), so it
    means either a file deleted by hand afterwards or - just as likely, in a
    folder the human chose - files of his own that happen to share the name.
    There is no way to tell those apart from here, so main refuses rather than
    guessing, and --force is how he settles it.
    """
    if marker_path(outdir, name).exists():
        return 'interrupted'
    present = [p for p in output_paths(outdir, name) if p.exists()]
    if len(present) == 3:
        return 'complete'
    return 'partial' if present else 'none'


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


class PublishError(RuntimeError):
    """The transcript could not be saved where it was asked for. Carries the fix.

    Kept separate from whatever faster-whisper raises, because the two need
    opposite advice: a decode failure sends the human back to the recording,
    while this one sends him to the output folder. Telling him the wrong one
    has him re-recording a perfectly good interview.
    """


def _discard(path):
    """Delete a working file, quietly.

    Every caller is tidying up after something else - either a success (the
    temps did their job) or a failure (whose exception is the real news). A
    complaint from the wastebasket must never replace either one.
    """
    try:
        Path(path).unlink()
    except OSError:
        pass


def _destination_problem(final):
    """Why this transcript could not be moved onto its final name, or None.

    Plain language and a way out, because the human reading it is a genealogist
    with a folder open, not a programmer: name the file, say what is wrong with
    it, and name the flag that avoids it.
    """
    if final.is_dir():
        return (f"there is a FOLDER named {final.name} in {final.parent}, so the transcript "
                f"cannot be saved under that name. Move or rename that folder, or run the "
                f"command again with a different --name or --outdir")
    if final.exists() and not os.access(final, os.W_OK):
        return (f"{final.name} in {final.parent} is read-only, so it cannot be replaced. "
                f"Make it writable or move it out of the way, or run the command again with "
                f"a different --name or --outdir")
    if not os.access(final.parent, os.W_OK | os.X_OK):
        return (f"the output folder {final.parent} cannot be written to. Run the command "
                f"again with --outdir pointing at a folder you can write to")
    return None


def _preflight_destinations(finals):
    """Refuse the whole run if any one of the three names cannot be replaced.

    `os.replace` is atomic for a single file, and there is no atomic multi-file
    rename anywhere this script runs. Renaming them one after another therefore
    has a real failure shape: the first lands, the second is refused, and the
    folder is left holding half of one run and half of another - which the
    skip-if-present rule then treats as a finished transcript forever. Asking
    every destination first turns that into a refusal before anything moves.

    Called twice: once on entry, so a doomed run costs a second instead of an
    hour of decoding, and again immediately before the first rename, because a
    folder is a live thing and an hour is long enough for someone to drop a
    folder or a read-only file onto one of these names.
    """
    for final in finals:
        problem = _destination_problem(final)
        if problem is not None:
            raise PublishError(problem)


def _set_aside(final):
    """Move an existing transcript out of the way, returning where it went.

    A rename, not a copy: it is atomic, it costs nothing on a big file, and it
    keeps the previous transcript byte-for-byte in case this run has to put it
    back. The sibling lives in the same folder so the move cannot cross a
    filesystem boundary and degrade into a copy that could half-finish.
    """
    fd, aside = tempfile.mkstemp(dir=str(final.parent), prefix=final.name + '.', suffix='.kept')
    os.close(fd)
    os.replace(final, aside)
    return Path(aside)


def _restore_previous(placed, kept):
    """Put the folder back the way it was. True if that fully succeeded.

    Undoing a half-finished promotion means two things: drop the files this run
    already published, then move each previous file back onto its name. A file
    that could not be dropped keeps its name occupied, so its predecessor is
    left aside rather than stacking a second failure on the first - and the
    caller is told the folder is NOT back to normal, which is what keeps the
    marker in place for the next run to find.
    """
    healed = True
    for final in placed:
        try:
            final.unlink()
        except OSError:
            healed = False
    for final, aside in kept:
        if final.exists():
            healed = False
            continue
        try:
            os.replace(aside, final)
        except OSError:
            healed = False
    return healed


def _promote_all(temps, finals, marker):
    """Rename the finished temps onto the three real names: all of them, or none.

    This function owns the whole commit, because a commit split across callers
    is how mixed states are born. In order:

      1. Write `marker` - from here until the last rename, anyone looking at
         this folder is told the set may be mid-change.
      2. For each pair: move the existing file aside (recoverable), then rename
         the temp onto the name.
      3. Remove the set-aside files, then the marker. Only now is the run done.

    Any failure - an OSError from a rename, or a Ctrl-C between two of them -
    rolls every step back and re-raises the original. If the rollback itself
    fully succeeded the marker goes too, because the folder is genuinely back to
    its previous state and the next run should be free to skip it. If it did
    not, the marker STAYS: that is the durable signal that turns a mixed set
    from a permanent lie into a job the next run redoes.

    What this cannot cover is a hard kill (SIGKILL, power loss) between two
    renames - no user-space code can. That case is exactly why the marker is
    written to disk first rather than tracked in memory: the marker survives the
    kill, and `publication_state` reads it.
    """
    try:
        marker.write_text(_MARKER_TEXT + ''.join(f"  {f.name}\n" for f in finals),
                          encoding='utf-8')
    except OSError as e:
        # Nothing has moved yet, so this is a clean refusal - but it has to be a
        # spoken one, not a raw OSError wearing main's decode-failure message.
        raise PublishError(
            f"the transcripts could not be saved into {marker.parent} "
            f"({e.strerror or e}). Check that the folder still exists and has room, then "
            f"run the same command again") from e
    kept = []
    placed = []
    failing = finals[0]
    try:
        for tmp, final in zip(temps, finals):
            failing = final
            if final.exists():
                kept.append((final, _set_aside(final)))
            os.replace(tmp, final)
            placed.append(final)
    except OSError as e:
        if _restore_previous(placed, kept):
            _discard(marker)
        # Translated, not swallowed: a bare OSError here would reach the human
        # through main's decode-failure message and send him off to check a
        # recording that transcribed perfectly well.
        raise PublishError(
            f"{failing.name} could not be saved into {failing.parent} "
            f"({e.strerror or e}). Check that the folder still exists and has room, and "
            f"that nothing else has that file open, then run the same command again") from e
    except BaseException:
        # BaseException on purpose: a Ctrl-C landing between two renames leaves
        # precisely the mixed set this function exists to prevent.
        if _restore_previous(placed, kept):
            _discard(marker)
        raise
    for _final, aside in kept:
        _discard(aside)
    _discard(marker)


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
    are removed and no output file exists at all.

    Putting three files in place is itself a multi-step change, so it is not
    done bare: destinations are checked first (`_preflight_destinations`) and
    the renames run under a commit protocol that can undo itself and leaves a
    marker behind when it cannot (`_promote_all`). The outcome is one of three,
    never a fourth: the new transcript, the previous state, or - only after a
    hard kill - a marked folder the next run redoes instead of skipping.

    A run that finds NO speech publishes nothing, for the same reason: an empty
    transcript is indistinguishable from a finished one to the next batch pass.
    The caller reports that (exit code 1) rather than leaving a stump on disk.

    `progress` is an optional callable taking the segment start time, so the
    console ticker stays out of the writing logic (and out of the tests).
    """
    outdir = Path(outdir)
    if not outdir.is_dir():
        raise PublishError(f"the output folder {outdir} is not there any more. Create it, or "
                           f"run the command again with --outdir pointing somewhere else")
    finals = output_paths(outdir, name)
    _preflight_destinations(finals)
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
        # Re-checked here, not just on entry: the transcription in between can
        # have taken an hour, and this is the last moment a refusal is free.
        _preflight_destinations(finals)
        _promote_all(temps, finals, marker_path(outdir, name))
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
            _discard(tmp)


def prepare_audio(src, workdir):
    """Return the path faster-whisper should read: a 16kHz mono wav, or the original.

    ffmpeg is the fast, forgiving decoder for phone containers, but it is an
    extra install a family archivist may not have. When it is missing or fails,
    faster-whisper's bundled PyAV reads the original file directly - slower and
    fussier with odd containers, but it keeps the skill usable with one pip
    install and no system packages.

    A zero exit code is not proof of a usable wav, so the file is checked as
    well: some builds of ffmpeg report success on a container whose audio track
    they could not read, and handing whisper an empty wav produces a confident
    transcript of nothing - a silent wrong answer, which is worse than the
    slower fallback.
    """
    wav = Path(workdir) / 'audio.wav'
    try:
        print("extracting audio (ffmpeg)...")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                        "-vn", "-ac", "1", "-ar", "16000", str(wav)], check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print(f"  ffmpeg unavailable ({e.__class__.__name__}); decoding with PyAV instead")
        return Path(src)
    if wav.is_file() and wav.stat().st_size > 0:
        return wav
    print("  ffmpeg found no audio to extract; decoding the original with PyAV instead")
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
                    help="re-transcribe, and overwrite files already sitting under this "
                         "run's output names - whether they are a finished transcript or "
                         "files this program cannot show it wrote")
    return ap


def rerun_command(argv, *extra):
    """This same run with `extra` on the end, as one line he can copy.

    Naming a flag is not naming a command: the human reading this has a folder
    open, not a terminal history, and re-typing an invocation with a path and
    three flags in it at 11pm is where the typos come from. So the refusal hands
    back what he ran, plus the flag that changes the answer.

    `sys.argv[0]` is used only when it really names THIS file, so the path he
    actually typed comes back to him. Under a test runner or any other embedding
    it names something else entirely (pytest's own `__main__.py`, say), and
    echoing that back would hand him a command that runs the wrong program - so
    the bare filename is the fallback.
    """
    args = list(argv) if argv is not None else list(sys.argv[1:])
    myself = Path(__file__).name
    invoked = sys.argv[0] if sys.argv else ''
    script = invoked if invoked and Path(invoked).name == myself else myself
    return 'python ' + shlex.join([script, *args, *extra])


def _leftover_note(outdir, name):
    """One sentence about what a run that did not finish actually left on disk.

    Read from the folder rather than remembered from the code path, because the
    two can disagree: the rollback in `_promote_all` may itself have failed, and
    a message that says "nothing was written" over a folder that was written to
    sends the human off to check the wrong thing. Whatever is on disk is what he
    is told.
    """
    state = publication_state(outdir, name)
    if state == 'interrupted':
        return (f"Careful: the transcript files in {outdir} can now be a mix of this run and "
                f"the last one. Run the same command again - it will rewrite all three rather "
                f"than skip them.")
    if state in ('complete', 'partial'):
        return f"The files already in {outdir} were left exactly as they were."
    return "Nothing was written - no half-finished transcript was left behind."


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
    state = publication_state(outdir, name)
    if state == 'complete' and not a.force:
        # Deliberately a success: this is what lets a long queue be re-run after
        # a reboot without re-doing hours of finished work. Only a COMPLETE set
        # counts as done - a half-published one is redone below, because a
        # skipped mixed set would never be repaired.
        print(f"already transcribed - these files are in {outdir}:")
        for p in finals:
            print(f"  {p.name}")
        print("Nothing was overwritten. Re-run with --force to replace them, "
              "or pass a different --name.")
        return EXIT_OK
    if state == 'partial' and not a.force:
        # A part-set with no marker is NOT evidence of an unfinished run of
        # ours. This program's own torn promotion always leaves the marker -
        # that is what the marker is for - and a run killed while writing leaves
        # only `.part` siblings, never a final name. So there is nothing here
        # saying these files came from a transcription at all, and --outdir is a
        # folder the human chose: a `rec.md` of his own, sitting where this run
        # wants to put `rec.md`, is an ordinary thing to find. Replacing it on a
        # guess is precisely the overwrite AGENTS.md forbids, so the run stops
        # and he decides which it is.
        print(f"the folder {outdir} already holds files named after this recording, but "
              "not a whole transcript:", file=sys.stderr)
        for p in finals:
            if p.exists():
                print(f"  {p.name}", file=sys.stderr)
        print("A transcript from this program is all three of .txt, .srt and .md together, "
              "and a run that was cut off part way leaves a marker file behind. Neither is "
              "true here, so there is nothing to say this program wrote them - and they have "
              "not been touched.", file=sys.stderr)
        print("If they are leftovers you want replaced, this redoes the recording and writes "
              "over them:", file=sys.stderr)
        print(f"  {rerun_command(argv, '--force')}", file=sys.stderr)
        print("If they are your own files, keep them and give the transcript a name of its "
              "own instead: add --name <something-else>, or --outdir <another folder>.",
              file=sys.stderr)
        return EXIT_FAILED
    if state == 'interrupted' and not a.force:
        print(f"an earlier run was stopped while it was putting this recording's transcript "
              f"files into {outdir}, so what is there can be a mix of two runs.")
        print("Redoing the recording now and rewriting all three. Once the new transcript "
              "looks right, the leftover files in that folder (the ones ending .part, .kept, "
              "or .publishing) can be deleted.")

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

        def tick(start):
            sys.stdout.write(f"\r  {fmt_ts(start)} transcribed")
            sys.stdout.flush()

        language = None if a.language.strip().lower() in ('', 'auto') else a.language
        try:
            # The ffmpeg pass is inside the try as well: it is minutes long on a
            # two-hour recording, which is plenty of time for a Ctrl-C, and a
            # traceback is never an acceptable thing to show for one.
            audio = prepare_audio(src, td)
            print(f"transcribing with faster-whisper '{a.model}' (this is the long part)...")
            model = WhisperModel(a.model, compute_type="auto")
            segments, _info = model.transcribe(str(audio), language=language, vad_filter=True)
            count = publish_transcripts(outdir, name, segments, a.model, src.name,
                                        progress=tick)
        except KeyboardInterrupt:
            print(f"\nstopped before the transcript was finished. "
                  f"{_leftover_note(outdir, name)}\n"
                  "Run the same command again when you are ready to start this recording over.",
                  file=sys.stderr)
            return EXIT_FAILED
        except PublishError as e:
            # Saving failed, not decoding. Different problem, different next
            # step, so it gets its own message - and one that does not guess how
            # far the run got, since this fires both before the first segment
            # (the destination was already blocked) and after the last one.
            print(f"\nthe transcript could not be saved: {e}.", file=sys.stderr)
            print(_leftover_note(outdir, name), file=sys.stderr)
            return EXIT_FAILED
        except Exception as e:
            # No traceback for the human: name the cause and the next move.
            print(f"\ntranscription failed: {e}", file=sys.stderr)
            print(_leftover_note(outdir, name), file=sys.stderr)
            print("Try: play the recording to confirm it is not corrupt, then run the same "
                  "command with --model small; if that works, the larger model ran out of "
                  "memory.", file=sys.stderr)
            return EXIT_FAILED

    if count == 0:
        print("\nno speech was found, so nothing was written.")
        if publication_state(outdir, name) != 'none':
            # A redo of an unfinished set promised to replace it. Silence means
            # that did not happen, and the folder still holds the old files -
            # say so, or he will take them for the new transcript.
            print(_leftover_note(outdir, name))
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

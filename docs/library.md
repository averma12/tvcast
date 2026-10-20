# Your library

Videos come from `~/Movies` by default. Manage folder tags under **Folders**; the cross removes
a folder from the library, without deleting its files.
Use **Rescan**, reopen the panel or run `tv open` to find new videos. Search by title or year;
**Continue watching** shows unfinished movies with saved progress.

**Skipped Movies/TV — macOS restricted access** means that subfolder could not be read.
Other folders are still scanned. Keep ordinary video files in a separate readable folder.

## Folder layout

```
Movies/
├── Some Film (2001)/                 one video in a folder: the folder name is the title
│   ├── some.film.2001.1080p.mkv
│   └── some.film.2001.1080p.srt      subtitles: named like the video
├── Some Saga/                        a folder of movie folders is a collection
│   ├── Part One (1999)/part.one.mp4
│   └── Part Two (2002)/part.two.mp4
└── Another.Film.2010.mp4             a loose file: the file name is the title
```

- **Titles:** dots and `[tags]` are cleaned up, and a year like `(2001)` is pulled out.
- **Subtitles:** see [Subtitles](#subtitles).
- **Skipped:** samples, extras folders (Featurettes, Extras, Trailers, …) and hidden files.
- **File types:** mp4, m4v, mov, mkv, avi, webm, ts, m2ts, mpg, mpeg, wmv.

## Subtitles

Subtitle files work on Samsung TVs: `.srt`, `.ass`, `.ssa` and `.vtt`, in any language. TV Cast
also sends them the ways Android TV renderers (Sony, Philips, Kodi) read them, but that is
untested on one. TV Cast lists these files for a video:

- files named like it, beside it or in `Subs/`: `Film.srt`, `Film.en.srt`, `Film.fr.forced.srt`, `Film.en.sdh.ass`;
- files in `Subs/Film/` (episode packs);
- for a video alone in its folder, every other subtitle file beside it and in `Subs/`.

The language and SDH, forced and commentary flags come from the file name. The language can be
a code (`en`, `eng`) or a name (`English`). `hi` straight after a language means
hearing-impaired; on its own it means Hindi.

**What plays by default:**

1. the subtitles you last chose for that movie, including Off;
2. otherwise, subtitles inside the video file, which the TV shows by itself;
3. otherwise, a file beside the video named exactly like it (`Film.srt`, `Film.ass`, …);
4. otherwise, none. Choose one in the panel or with `tv subs`.

**While a movie plays:** the panel's **Subtitles** menu switches files instantly on a Samsung;
other TVs reload the video where it is, with a pause of a few seconds. **Add file…**
attaches any subtitle file on your Mac to the movie; TV Cast remembers it, and **Remove** detaches
it. From the terminal:

```sh
tv subs                     # list them; * marks the one showing
tv subs french              # switch by language, number or part of the name
tv subs off                 # or on
tv subs --add ~/Downloads/film.en.srt
tv open dune --subs ~/Downloads/dune.srt    # or --subs french, --subs off
```

**Subtitles inside the video** (MKV and MP4 tracks) are shown and switched by the TV itself:
use its own subtitle menu. A subtitle file chosen in TV Cast replaces them, and TV Cast's
**Off** doesn't hide them.

**Blu-ray image subtitles** (PGS) inside the video may not show at all. Convert them to a text
file with an OCR tool such as Subtitle Edit, or burn them into the picture. Burning in re-encodes
the whole video:

```sh
ffmpeg -i in.mkv -filter_complex "[0:v][0:s:0]overlay[v]" -map "[v]" -map 0:a:0 -c:a copy out.mp4
```

Text files in other encodings (Windows-1252, UTF-16) work. If a file shows the wrong
characters, save it as UTF-8.

## Resume and queue

- A movie resumes a few seconds before where you stopped. Use **Start over** or
  `tv open … --from-start` to begin again.
- Past 95% counts as watched.
- **Queue** adds a movie without starting it. Use **Play queue** to start, **Play next** to skip,
  the arrows to reorder, or the cross to remove an entry. **Clear** leaves the current movie playing.
- The next queued movie starts when the current cast finishes. Stop, TV changes and disconnections
  leave the remaining queue intact. An empty queue ends playback.
- The queue survives restarts. Unavailable videos stay listed; reconnect their folder and rescan,
  or remove them. Failed playback keeps the entry for retry.
- The Mac stays awake while a movie plays. Closing the lid still puts it to sleep.

## Videos the TV can't play

With `ffprobe` installed (`brew install ffmpeg`), each video is checked once. A video the TV likely
can't decode shows **Try anyway** instead of **Play**.

The most common case is DTS or TrueHD audio on Samsung TVs, which plays silent. To fix a file,
convert just its audio:

```sh
ffmpeg -i in.mkv -map 0:v:0 -map 0:a:0 -c:v copy -c:a ac3 -b:a 640k out.mkv
```

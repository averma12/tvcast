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
│   └── some.film.2001.1080p.srt      subtitles: same name as the video
├── Some Saga/                        a folder of movie folders is a collection
│   ├── Part One (1999)/part.one.mp4
│   └── Part Two (2002)/part.two.mp4
└── Another.Film.2010.mp4             a loose file: the file name is the title
```

- **Titles:** dots and `[tags]` are cleaned up, and a year like `(2001)` is pulled out.
- **Subtitles** (`.srt`): the first match wins:
  1. a file with the video's name;
  2. for a video alone in its folder, the only `.srt` beside it;
  3. for a video alone in its folder, an English one in a `Subs/` folder.
- **Skipped:** samples, extras folders (Featurettes, Extras, Trailers, …) and hidden files.
- **File types:** mp4, m4v, mov, mkv, avi, webm, ts, m2ts, mpg, mpeg, wmv.

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

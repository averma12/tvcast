# Your library

Videos come from `~/Movies` by default. Add or remove folders under **Library folders & auto-play**.
Use **Rescan**, reopen the panel or run `tv open` to find new videos. Search by title or year;
**Continue watching** shows unfinished movies with saved progress.

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

## Resume and auto-play

- A movie resumes a few seconds before where you stopped. Use **Start over** or
  `tv open … --from-start` to begin again.
- Past 95% counts as watched.
- **Auto-play** (under **Library folders & auto-play**) starts the next movie of a collection when one ends.
- The Mac stays awake while a movie plays. Closing the lid still puts it to sleep.

## Videos the TV can't play

With `ffprobe` installed (`brew install ffmpeg`), each video is checked once. A video the TV likely
can't decode shows **Try anyway** instead of **Play**.

The most common case is DTS or TrueHD audio on Samsung TVs, which plays silent. To fix a file,
convert just its audio:

```sh
ffmpeg -i in.mkv -map 0:v:0 -map 0:a:0 -c:v copy -c:a ac3 -b:a 640k out.mkv
```

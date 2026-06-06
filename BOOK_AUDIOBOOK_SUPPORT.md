# Book and Audiobook Support

This document describes the book/audiobook changes in this fork of
Easynews-as-indexer, how to test them, and what to expect in Chaptarr,
Prowlarr, and Readarr.

## Original Limitations

The original bridge was effectively video-only:

- Newznab caps exposed Movies and TV, but category `7000` was advertised as
  generic `Other` instead of Books.
- All EasyNews searches were forced through `fty[]=VIDEO`, even when the
  client requested book-related categories.
- Result filtering rejected anything that was not an allowed video extension.
- Result filtering rejected non-`VIDEO` EasyNews result types.
- A minimum size of 100 MB was applied by default, which is too high for many
  ebooks.
- Category detection assumed non-TV results were movies, so ebook files with a
  year in the title could be categorized as Movies.
- The bridge did not log the final EasyNews search URL, making it hard to see
  whether the upstream query was still constrained to video.

Because of this, book and audiobook posts visible on the EasyNews website could
be invisible through the bridge.

## Changes Made

### Newznab Caps

Caps now advertise Books:

- `7000` Books
- `7010` Books/EBook
- `7040` Books/Audiobook

### Category-Aware Search Profiles

Search behavior now depends on the requested Newznab category:

- Movie/TV searches still use `fty[]=VIDEO`.
- Generic video searches without book categories still use `fty[]=VIDEO`.
- EBook searches, `cat=7010`, do not force `fty[]=VIDEO`.
- Audiobook searches, `cat=7040`, use `fty[]=AUDIO`.
- Top-level Books searches, `cat=7000`, do not force a file type filter so both
  ebook and audiobook candidates can be seen.

### Extension Detection

The bridge now allows these ebook extensions:

- `.epub`
- `.mobi`
- `.azw3`
- `.pdf`

The bridge now allows these audiobook extensions:

- `.mp3`
- `.m4b`

### Book Category Detection

Book/audiobook category detection runs before movie/TV detection.

Detection uses:

- file extensions
- title markers such as `ebook`, `e-book`, `audiobook`, `audio book`
- EasyNews group/category fields when they are present in the JSON response

### Size and Duration Filtering

For book categories:

- default minimum size is `0 MB`
- video duration filtering is disabled

For movie/TV categories:

- default minimum size remains `100 MB`
- video duration filtering remains enabled

### Logging

The EasyNews client now logs:

- the final EasyNews search URL
- the raw EasyNews result count

The server also logs:

- mapped result count after local filtering
- search category profile used by the request

### Request Hardening

The EasyNews client now includes request timeouts and wraps network errors for:

- login
- search
- NZB download

The `/api?t=get` NZB fetch path also includes timeout/error handling.

## How To Test

Start the server:

```powershell
python server.py
```

Use your configured API key. The default is `testkey`.

### Caps

```powershell
Invoke-WebRequest "http://127.0.0.1:8081/api?t=caps&apikey=testkey" | Select-Object -ExpandProperty Content
```

Expected:

- category `7000` named `Books`
- subcategory `7010` named `Books/EBook`
- subcategory `7040` named `Books/Audiobook`

### EBook Validation Search

```powershell
Invoke-WebRequest "http://127.0.0.1:8081/api?t=search&q=test&cat=7010&apikey=testkey" | Select-Object -ExpandProperty Content
```

Expected:

- RSS response
- sample result categorized as `7010`

### Audiobook Validation Search

```powershell
Invoke-WebRequest "http://127.0.0.1:8081/api?t=search&q=test&cat=7040&apikey=testkey" | Select-Object -ExpandProperty Content
```

Expected:

- RSS response
- sample result categorized as `7040`

### Live EBook Search

```powershell
Invoke-WebRequest "http://127.0.0.1:8081/api?t=search&q=<book title>&cat=7010&apikey=testkey" | Select-Object -ExpandProperty Content
```

Expected EasyNews URL behavior in logs:

- no `fty%5B%5D=VIDEO`

Expected result behavior:

- `.epub`, `.mobi`, `.azw3`, and `.pdf` results may be returned
- results should be categorized as `7010`

### Live Audiobook Search

```powershell
Invoke-WebRequest "http://127.0.0.1:8081/api?t=search&q=Dungeon%20Crawler%20Carl&cat=7040&apikey=testkey" | Select-Object -ExpandProperty Content
```

Expected EasyNews URL behavior in logs:

- `fty%5B%5D=AUDIO`
- no `fty%5B%5D=VIDEO`

Expected result behavior:

- `.mp3` and `.m4b` results may be returned
- results should be categorized as `7040`

### Broad Books Search

```powershell
Invoke-WebRequest "http://127.0.0.1:8081/api?t=search&q=Dungeon%20Crawler%20Carl&cat=7000&apikey=testkey" | Select-Object -ExpandProperty Content
```

Expected EasyNews URL behavior in logs:

- no `fty%5B%5D=VIDEO`
- no forced single media type

Expected result behavior:

- ebook and audiobook extensions may be returned
- `.mp3` and `.m4b` results should be categorized as `7040`
- ebook extensions should be categorized as `7010`

## Expected Behavior In Clients

### Prowlarr

Add or refresh this bridge as a Generic Newznab indexer.

Expected:

- Caps should show Books categories.
- Prowlarr should be able to test the indexer with `cat=7010` or `cat=7040`.
- Searches from Readarr or Chaptarr routed through Prowlarr should include the
  requested book category.
- Movie and TV apps should continue to behave as before.

Useful checks:

- Review bridge logs during a Prowlarr test.
- Confirm movie/TV searches still include `fty[]=VIDEO`.
- Confirm ebook/audiobook searches do not include `fty[]=VIDEO`.

### Readarr

Readarr typically uses Newznab book categories for ebook searches.

Expected:

- EBook searches should use `cat=7010` or `cat=7000`.
- The bridge should stop filtering out `.epub`, `.mobi`, `.azw3`, and `.pdf`.
- Small ebooks should no longer be removed by the old 100 MB minimum.

Limitations:

- If Readarr requests a category other than `7000` or `7010`, the bridge may use
  the normal video profile unless that category is added later.
- Archive/container posts are still not handled as ebooks unless EasyNews
  exposes the final file extension as one of the supported ebook extensions.

### Chaptarr

Chaptarr audiobook searches are expected to use audiobook-oriented Newznab
categories.

Expected:

- Audiobook searches should use `cat=7040` or possibly top-level `cat=7000`.
- `cat=7040` searches should send `fty[]=AUDIO`.
- `cat=7000` searches should avoid a forced EasyNews media type and may be more
  useful when EasyNews does not classify an audiobook post as `AUDIO`.
- `.mp3` and `.m4b` results should be categorized as `7040`.

Limitations:

- Audiobook posts that appear on the EasyNews website as `.rar`, `.zip`, `.7z`,
  split archives, or other container formats are still likely to be filtered out.
- If an audiobook is visible on the website but not returned with
  `fty[]=AUDIO`, try testing with `cat=7000` to see whether the broader Books
  search exposes it.
- If EasyNews only exposes group data in a list format without newsgroup fields,
  title and extension detection become the primary signals.

## Known Remaining Gaps

- Live EasyNews behavior depends on how the EasyNews API classifies each post.
- Audiobook-only `cat=7040` may miss posts that are audiobooks but not typed as
  `AUDIO` by EasyNews.
- Archive-based audiobook posts are not yet supported.
- The bridge does not yet inspect archive contents.
- The bridge does not yet support additional book-related Newznab categories
  beyond `7000`, `7010`, and `7040`.

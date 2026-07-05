# Time Per Deck (Anki add-on)

Single-file Anki add-on (`__init__.py`) that switches decks to time-based
studying. No test suite; sanity-check changes with
`python -m py_compile __init__.py`.

## Packaging

`build.sh` zips the add-on into `TimePerDeck.ankiaddon` for upload to AnkiWeb
(https://addon-docs.ankiweb.net/sharing.html). Rules: files go at the zip's
top level (no wrapping folder), and the zip must never include `meta.json`,
`__pycache__`, or `.pyc` files. If you add new runtime files to the add-on,
add them to the file list in `build.sh` too.

The GitHub Actions workflow `.github/workflows/build-ankiaddon.yml` runs
`build.sh` on every pull request and uploads `TimePerDeck.ankiaddon` as a
workflow artifact, so each PR has a ready-to-upload build attached.

IMPORTANT: `TimePerDeck.ankiaddon` is also committed to the repository so the
user can download it directly from the GitHub file page without unzipping an
artifact. Whenever you change any add-on file, run `bash build.sh` and include
the regenerated `TimePerDeck.ankiaddon` in the same commit before pushing.

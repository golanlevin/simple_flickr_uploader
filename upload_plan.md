# Flickr Upload Plan

This is a small, reusable plan for uploading local folders of images to Flickr albums with the Flickr API.

The example repository expects this structure:

```text
.
├── flickr_uploader.py
├── media/
│   ├── albums.md
│   ├── kittens/
│   │   ├── kitten1.jpg
│   │   └── kitten1.jpg.json
│   └── snails/
│       ├── snail.jpg
│       └── snail.jpg.json
└── requirements.txt
```

Each folder inside `media/` becomes one Flickr album/photoset. Each media file can have a sidecar JSON file named by appending `.json` to the media filename, for example `kitten1.jpg.json`.

## Album Metadata

Album metadata lives in `media/albums.md`.

Each album section begins with a top-level heading matching a folder in `media/`:

```markdown
# kittens

Title: Some Nice Kittens

Credit: Anonymous

Here are some nice kittens from the internet.

Info: https://example.com/
```

Rules:

- The heading must match the media folder name.
- `Title:` becomes the Flickr album title.
- `Credit:` is optional local metadata, but if present it is placed at the top of the Flickr album description without the `Credit:` label.
- The remaining text becomes the Flickr album description.
- The literal labels `Title:` and `Credit:` are not uploaded.

## Photo Metadata

Each sidecar JSON can contain:

```json
{
  "Title": "A useful photo title",
  "Description": "A photo description.",
  "Tags": "example tag words"
}
```

Rules:

- Use `Title` as the Flickr photo title when present.
- If `Title` is missing, derive a readable title from the filename.
- If `Title` looks like a camera filename such as `IMG_4419`, `DSC00335`, or `P1000336`, prepend the album title.
- Use `Description` as the Flickr photo description when present.
- If `Description` is missing, use a fallback built from the album title and credit.
- Use `Tags` when present.
- If `Tags` is missing, use common tags from the same album when possible; otherwise use the global fallback tags in the script.
- Ignore JSON sidecars that do not have matching media files.

## Authentication

1. Log in to the Flickr account that should own the uploaded photos.
2. Open Flickr's API key page:

   https://www.flickr.com/services/api/keys/

3. If you do not already have an API key, create one here:

   https://www.flickr.com/services/apps/create/

4. Choose the key type that matches your use. A personal archive uploader is usually non-commercial.
5. When Flickr asks what you are building, describe the real workflow. For example:

   ```text
   I am building a local Python script that uploads my own image folders
   to my own Flickr account, creates albums, and applies photo metadata
   from local JSON sidecar files. It is not a public web app or a service
   for other users.
   ```

6. After Flickr creates the app, copy the API key and API secret.
7. Put the key and secret in `.flickr_api_credentials.json`:

   ```json
   {
     "api_key": "YOUR_API_KEY",
     "api_secret": "YOUR_API_SECRET"
   }
   ```

8. Never commit `.flickr_api_credentials.json` or `.flickr_tokens.json`.
9. Run OAuth once:

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   .venv/bin/python flickr_uploader.py auth
   ```

10. Open the printed Flickr authorization URL, approve write access, and paste the verifier code back into the terminal.

The script uses Flickr OAuth 1.0a with `write` permission. After authorization, it writes `.flickr_tokens.json`, which lets later upload commands run without repeating the browser authorization step.

## Validate First

Run:

```bash
.venv/bin/python flickr_uploader.py validate --show-issues
```

Validation checks:

- every album folder in `media/` has a `media/albums.md` section
- every album section has a matching folder
- required album title data exists
- sidecar JSON files are well-formed objects
- media files missing JSON are reported but still uploadable
- orphan JSON sidecars are reported and ignored

## Dry Run

Run:

```bash
.venv/bin/python flickr_uploader.py upload --all --dry-run
```

This does not contact Flickr. It reports how many albums and photos would be uploaded.

## Private Test Upload

Upload one album privately first:

```bash
.venv/bin/python flickr_uploader.py upload --album kittens --visibility private
```

Check the result on Flickr before uploading everything publicly.

## Public Upload

When ready:

```bash
.venv/bin/python flickr_uploader.py upload --all --visibility public --confirm-public-upload --image-delay 1 --album-delay 60
```

The public confirmation flag is intentional so a public upload cannot happen by accident.

The uploader saves `upload_state.json` after each photo. If the process is interrupted, rerun the same command and it will reuse already uploaded photos instead of duplicating them.

## Reports

After each album, the script writes a report to `upload_reports/`.

The reports include:

- album folder
- Flickr photoset ID
- uploaded photo IDs
- reused photo IDs
- errors, if any

## License And Safety

The example script applies Creative Commons BY-NC 4.0 by default.

- Flickr license id: `14`
- Flickr API method: `flickr.photos.licenses.setLicense`
- Safety level: `1`, meaning safe
- Content type: `1`, meaning photo

Change these defaults in `flickr_uploader.py` if your upload requires different settings.

## Useful Flickr API Methods

- Upload API: https://www.flickr.com/services/api/upload.api.html
- OAuth: https://www.flickr.com/services/api/auth.oauth.html
- Create photoset: https://www.flickr.com/services/api/flickr.photosets.create.html
- Edit photos in photoset: https://www.flickr.com/services/api/flickr.photosets.editPhotos.html
- Set photo metadata: https://www.flickr.com/services/api/flickr.photos.setMeta.html
- Set photo tags: https://www.flickr.com/services/api/flickr.photos.setTags.html
- Set photo license: https://www.flickr.com/services/api/flickr.photos.licenses.setLicense.html
- Get upload status: https://www.flickr.com/services/api/flickr.people.getUploadStatus.html

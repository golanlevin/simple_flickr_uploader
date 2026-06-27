# Simple Flickr Uploader

This is a small example project for uploading local image folders to Flickr albums with Python and the Flickr API.

Each folder inside `media/` becomes one Flickr album. Each image can have a sidecar JSON file with the same filename plus `.json`, such as `kitten1.jpg.json`.

Album metadata lives in `media/albums.md`.

## Get Flickr API Credentials

1. Log in to the Flickr account that will own the uploaded photos.
2. Open Flickr's API key page:

   https://www.flickr.com/services/api/keys/

   If you do not already have a key, use Flickr's app creation page:

   https://www.flickr.com/services/apps/create/

3. Choose a non-commercial key unless your use case is commercial.
4. Describe the app honestly. For a local one-time uploader, say that it is a local script for uploading your own photos to your own Flickr account.
5. After Flickr creates the app, copy the API key and API secret.
6. Create a local credentials file from the example:

   ```bash
   cp .flickr_api_credentials.example.json .flickr_api_credentials.json
   ```

7. Edit `.flickr_api_credentials.json`:

   ```json
   {
     "api_key": "YOUR_API_KEY",
     "api_secret": "YOUR_API_SECRET"
   }
   ```

Do not commit `.flickr_api_credentials.json`. It is ignored by `.gitignore`.

## Install And Authorize

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Then authorize Flickr write access:

```bash
.venv/bin/python flickr_uploader.py auth
```

## Validate

```bash
.venv/bin/python flickr_uploader.py validate --show-issues
```

## Dry Run

```bash
.venv/bin/python flickr_uploader.py upload --all --dry-run
```

## Upload One Private Test Album

```bash
.venv/bin/python flickr_uploader.py upload --album kittens --visibility private
```

## Upload Everything Publicly

```bash
.venv/bin/python flickr_uploader.py upload --all --visibility public --confirm-public-upload --image-delay 1 --album-delay 60
```

The uploader writes resumable state to `upload_state.json` and per-album reports to `upload_reports/`.

See `upload_plan.md` for the full workflow and metadata rules.

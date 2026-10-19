# tg-jellyfin-sync

Imports video and subtitle files posted to a private Telegram channel into the Jellyfin libraries.

1. Post a file to the channel (send as **File** to keep the original name and quality).
2. The bot downloads it, names it the way Jellyfin expects, moves it into `movies/` or `tvshows/`,
   asks Jellyfin to scan that one file, and replies ✅.
3. If the name can't be identified (phone clips, names without a year…), the file goes to `unsorted/`.
   Edit the post's caption to `#movie Title 2010` or `#tv Show S01E02` and the bot files it.

A caption tag on the original post always overrides the filename. Files over 2 GiB are rejected,
duplicates are detected by Telegram file id, and with `DELETE_AFTER_IMPORT` the original post is
removed after a successful import.

## Local run

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env   # fill in the TG_* values; paths default to ./local
.venv/bin/pytest
.venv/bin/python -m app.main
```

Stop any deployed instance first — two copies on the same bot token would both import every post.

## Deployment

Pushes to `main` run the tests, build `prateeksavanur/tg-jellyfin-sync`, and pin the new tag in
`kubernetes/deployment.yaml`; ArgoCD (Application in the homelab repo) rolls it out.

- Runs on the media node and mounts `/mnt/media/library` at `/data`. Jellyfin mounts
  `library/movies` and `library/tvshows` at the same `/data/...` paths, so the per-file refresh
  resolves. Staging lives in the same mount so the final move is an atomic rename.
- Secrets (`tg-jellyfin-sync-secrets`) are created by Ansible: `roles/tg_jellyfin_sync` in the homelab repo.
- Failed imports are sent to Homelert (`/webhook/tg-jellyfin-sync`).
- GitHub repo secrets needed by CI: `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`.

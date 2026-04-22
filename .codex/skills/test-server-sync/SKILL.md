---
name: test-server-sync
description: Sync and verify the to_text project on the test server 202.140.140.117. Use when you need to pull latest code into /data/to_text, restart the transcription service, patch Tencent ASR credentials/defaults, and validate /transcribe API with the fixed mp3 URL.
---

# Test Server Sync

Use this skill for repeatable deployment/verification on the test server.

## Fixed Environment

- Default host: `202.140.140.117`
- Default user: `root`
- Remote project dir: `/data/to_text`
- Local source dir (for reference): `/data/project/to_text`
- Branch: `main`
- API sample URL: `https://df.qi.work/fs/file/phone/2067/2026/04/44da98e4-6963-4999-b112-14496f336c48.mp3`

## Environment Variables

The sync script reads credentials from environment variables:

- `TEST_SERVER_HOST` - optional, defaults to `202.140.140.117`
- `TEST_SERVER_USER` - optional, defaults to `root`
- `TEST_SERVER_PASSWORD` - required, SSH password for the server
- `TEST_SERVER_REMOTE_DIR` - optional, defaults to `/data/to_text`
- `TEST_SERVER_BRANCH` - optional, defaults to `main`
- `TEST_SERVER_AUDIO_URL` - optional, defaults to the sample MP3 URL used for verification

If `TEST_SERVER_PASSWORD` is not set, the script exits immediately with a clear error.

## Workflow

1. Export the required environment variable:
```bash
export TEST_SERVER_PASSWORD='your-ssh-password'
```
2. Run the sync script from this skill folder:
```bash
bash scripts/sync_restart_verify.sh
```
3. Confirm remote HEAD matches expected commit and `/health` plus `/transcribe` both return success.
4. For verify-only runs, reuse the same environment variables and run:
```bash
bash scripts/sync_restart_verify.sh --verify-only
```

## Commands

- Full sync + restart + verify:
```bash
bash scripts/sync_restart_verify.sh
```
- Sync only:
```bash
bash scripts/sync_restart_verify.sh --sync-only
```
- Verify only:
```bash
bash scripts/sync_restart_verify.sh --verify-only
```

## Notes

- This server is test-only.
- `transcribe_config.template.json` is committed as the template; remote deploy materializes `transcribe_config.json` before restart.
- Script uses `sshpass` when available; otherwise it exits with manual SSH instructions.
- The sync step preserves an existing remote `transcribe_config.json` by backing it up before resetting the repo and restoring it afterward.

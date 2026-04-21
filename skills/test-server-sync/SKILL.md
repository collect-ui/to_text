---
name: test-server-sync
description: Sync and verify the to_text project on the test server 202.140.140.117. Use when you need to pull latest code into /data/to_text, restart the transcription service, patch Tencent ASR credentials/defaults, and validate /transcribe API with the fixed mp3 URL.
---

# Test Server Sync

Use this skill for repeatable deployment/verification on the test server.

## Fixed Environment

- Host: `202.140.140.117`
- User: `root`
- Password: `Zhangzhi@888`
- Remote project dir: `/data/to_text`
- Local source dir (for reference): `/data/project/to_text`
- Branch: `main`
- API sample URL: `https://df.qi.work/fs/file/phone/2067/2026/04/44da98e4-6963-4999-b112-14496f336c48.mp3`

## Workflow

1. Run `scripts/sync_restart_verify.sh` from this skill folder.
2. Confirm remote HEAD matches expected commit.
3. Confirm `/health` and `/transcribe` both return success.
4. If password SSH fails, switch to interactive `ssh root@202.140.140.117` and run the same remote commands manually.

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
- `transcribe_config.template.json` is committed as the template; remote deploy should materialize `transcribe_config.json` before restart.
- Script uses `sshpass` when available; otherwise it falls back to interactive SSH instructions.

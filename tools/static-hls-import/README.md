# Static HLS backlog importer

This operator tool migrates the reviewed CPCS-202, CPCS-203, and CPIT-201
Google Drive videos into immutable static-HLS packages in Cloudflare R2. It is
for authorized KStack content only. It does not place Google or R2 credentials
in the repository and it does not proxy video bytes through the Devs backend.

The importer processes one lesson at a time so the Oracle VPS never needs to
hold the complete 78.7 GiB source library and all encoded outputs together.

## Safety model

For every lesson the runner:

1. Downloads one source from the read-only Drive remote.
2. Verifies its exact byte size and inspects it with FFprobe.
3. Encodes aligned H.264/AAC 880p and 660p HLS renditions.
4. Measures actual average and RFC peak bandwidth and writes the master.
5. Decodes both finished playlists and writes `SHA256SUMS.txt`.
6. Uploads segments and initialization files with immutable cache headers.
7. Uploads rendition playlists and verifies the unpublished package.
8. Uploads `master.m3u8` last as the publication marker.
9. Verifies the final R2 tree and public URL, including CORS.
10. Appends a durable `published` state and removes that lesson's temporary
    files. Failed workspaces are retained for diagnosis and resume.

The runner refuses to overwrite a remote prefix containing `master.m3u8`.
Changing an encoded lesson therefore requires a new versioned destination.

## VPS prerequisites

Ubuntu packages:

```bash
sudo apt update
sudo apt install ffmpeg rclone python3 tmux
```

Use a current official rclone release. The Google Drive shared client ID is
retiring during 2026, so configure a personal OAuth Desktop client. The source
remote should be named `devs-drive`, use the `drive.readonly` scope, and set its
advanced `root_folder_id` to the approved shared folder:

```text
1wXxsQmxJo4KGhB4e6L4lyKAKUp77fDrQ
```

The destination remote remains `devs-r2`. It requires object read/write access
to `devs-video-delivery-test`, but it does not require bucket-creation access.

Verify both remotes without printing their configuration:

```bash
rclone lsd devs-drive:
rclone lsf devs-r2:devs-video-delivery-test/pilots --dirs-only
```

Expected Drive directories include `CPCS-202`, `CPCS-203`, and `CPIT-201`.

## Create and review the real manifest

Never commit the real manifest. It contains the complete source inventory and
is ignored under this tool's `output/` directory.

On the VPS:

```bash
cd /srv/devs-video-import/tool
mkdir -p output

python3 build_manifest.py \
  --source-remote devs-drive: \
  --destination-remote devs-r2:devs-video-delivery-test/pilots \
  --public-base-url https://devs-video-test.fawazabdullah.dev/pilots \
  --cors-origin https://devs-staging.fawazabdullah.dev \
  --version 2026-08-28-v1 \
  --output output/three-series.json
```

The builder deliberately expects exactly 258 approved videos. It creates this
reviewable structure:

- CPCS-202: Slides, then Labs.
- CPCS-203: Slides, Midterm, Final Exam, then Final Lab.
- CPIT-201: Chapters 2–8 and 10, Appendices, Homework, then Exams and Review.

The 56 CPCS-203 PDF, ZIP, TXT, and PNG files are not encoded and are not part of
this manifest. They require a separate reviewed lesson-to-attachment mapping.

Review lesson titles, section order, source paths, byte sizes, and destination
paths before starting. An immutable version is shared by the batch, but every
lesson has its own directory.

## Rehearse one lesson

The dry run validates the manifest and prints the selected work without
downloading anything:

```bash
python3 encode_and_upload.py \
  --manifest output/three-series.json \
  --work-dir /srv/devs-video-import \
  --item cpcs-202-s01-l001 \
  --dry-run
```

Then encode that single lesson while reserving one of the VPS's four CPUs for
Dokploy and other services:

```bash
tmux new -s devs-video-import

python3 encode_and_upload.py \
  --manifest output/three-series.json \
  --work-dir /srv/devs-video-import \
  --item cpcs-202-s01-l001 \
  --cpu-set 0-2 \
  --keep-work
```

Detach from tmux with `Ctrl+B`, then `D`. Reattach with:

```bash
tmux attach -t devs-video-import
```

With `--keep-work`, inspect the local output, the printed public URL, playback,
seeking, manual quality switching, and the state entry before removing the
rehearsal workspace.

## Run the resumable batch

After approving the rehearsal, omit `--item` and `--keep-work`:

```bash
python3 encode_and_upload.py \
  --manifest output/three-series.json \
  --work-dir /srv/devs-video-import \
  --cpu-set 0-2
```

The append-only state is stored at:

```text
/srv/devs-video-import/state.jsonl
```

Rerunning the same command skips only items with a durable `published` entry.
`--limit 1` is useful for controlled batches. A failed item exits non-zero and
leaves `/srv/devs-video-import/items/<item-id>` plus its logs intact. Resolve
the cause, then either resume the unchanged work or assign a new version when
the encoded bytes must change.

## Register the lessons in Devs

Every `published` state row contains the public `manifestUrl`, measured duration,
segment count, and bandwidth metadata required to register the lesson through
the admin workflow. Registration is intentionally separate from publication so
a malformed title or curriculum mapping cannot silently expose content.

After the first full series is encoded, add its reviewed sections and lessons
in Devs, attach each manifest URL, verify playback in staging, and only then
publish the series. Bulk API registration can consume the same reviewed
manifest later; it must not bypass backend validation.

## Cleanup and revocation

Do not delete Drive originals. They remain the source masters.

After migration:

- Keep `state.jsonl`, the reviewed manifest, and logs until production cutover.
- Remove successfully published temporary item workspaces.
- Revoke the Google OAuth grant or remove the VPS-only rclone configuration if
  the VPS no longer needs Drive access.
- Never delete or mutate a published R2 version in place. Replace it with a new
  immutable version and let the Devs retention workflow retire the old media.

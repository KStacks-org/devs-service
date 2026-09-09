# KStack Devs Service

The backend for **KStack Devs**, KStack's free learning platform for practical technology courses and multi-lesson series. It owns the catalog, publication workflow, access policy, lesson attachments, and video metadata while large files travel directly between clients and Cloudflare R2.

## ✨ Features

- Course and series catalog with Arabic and English metadata
- Optional bilingual sections with ordered lessons for longer series
- Draft, publication, archive, and future-ready visibility policies
- Reusable instructor profiles and complete bilingual content metadata editing
- Seven-day content, lesson, and media trash with restore and safe object cleanup
- Thirty-day lesson video version history with rollback
- Validated static HLS playback with WebVTT captions
- Direct-to-R2 cover image uploads for JPEG, PNG, WebP, and AVIF
- Direct-to-R2 lesson attachment uploads with size and type enforcement
- Seven-day attachment deletion retention and automatic object cleanup
- Managed WebVTT caption uploads and caption metadata updates
- KStacks ES256 JWT and role integration
- PostgreSQL schema migrations through Flyway
- Kubernetes liveness and readiness endpoints

## 🧱 Stack

- Java 25 and Spring Boot 4
- Spring MVC, Security, Validation, and Data JPA
- PostgreSQL 17 and Flyway
- Cloudflare R2 through the AWS S3 SDK
- Maven, JUnit, AssertJ, and Mockito
- Docker, GHCR, and Kubernetes

## 🚀 Local development

Requirements: Java 25, Maven 3.9+, Docker with Compose, and the sibling `devs-frontend` repository for the full stack.

```bash
cp .env.example .env
docker compose up -d postgres
mvn spring-boot:run
```

The API starts at `http://localhost:8080/devs/api/v1`. Local admin endpoints remain locked unless `DEVS_ALLOW_INSECURE_ADMIN=true`; the production profile always disables that escape hatch.

Run verification with:

```bash
mvn verify
```

Run the complete containerized stack with:

```bash
docker compose up --build
```

## 🔌 API groups

| Prefix | Purpose |
|---|---|
| `/devs/api/v1/public/**` | Published home, catalog, and content responses |
| `/devs/api/v1/admin/content/**` | Content metadata, lesson, cover, trash, and publication operations |
| `PUT /devs/api/v1/admin/content/{id}/curriculum` | Atomically replace a series section and lesson order |
| `/devs/api/v1/admin/instructors/**` | Reusable instructor profile management |
| `/devs/api/v1/admin/media/**` | Media library, trash, static-HLS registration, and caption management |
| `/devs/api/v1/admin/units/{unitId}/attachments/**` | Attachment upload, confirmation, deletion, restore, and ordering |
| `/actuator/health/liveness` | Container and Kubernetes liveness probe |
| `/actuator/health/readiness` | Database-aware readiness probe |

## 📦 Attachment lifecycle

Attachments belong to a content unit: the single lesson in a course or an individual episode in a series.

1. An admin requests a short-lived signed upload URL.
2. The browser uploads the file directly to R2 using the returned signed headers.
3. The admin client confirms completion; the service checks the object and its exact byte size.
4. Only `READY` attachments appear in public content responses.
5. Deletion hides the attachment immediately and sets a purge time seven days ahead.
6. The scheduled purge removes the R2 object only after the retention window. Failures are logged and retried.

The initial policy accepts PDF, ZIP, Office documents, plain text, Markdown, common source-code files, PNG, JPEG, WebP, and GIF. Files are limited to 100 MiB and lessons to 20 active attachments by default. PDFs receive inline disposition; all other formats receive attachment disposition.

Because the chosen R2 custom domain is public, attachment URLs are public too. Keep `attachments/` on a dedicated media origin and never store private student information there. A future authenticated-download policy should use a private bucket or signed downloads rather than CORS as access control.

R2 CORS must allow the frontend origins, `GET`, `HEAD`, and `PUT`, and the `Content-Type` and `Content-Disposition` request headers. The exact policy is documented in [docs/architecture.md](docs/architecture.md#cloudflare-r2-uploads).

## 🗂️ Series curricula

A series may remain flat or contain one level of ordered sections. Each section has a required English title plus optional Arabic title and bilingual descriptions. Lessons retain one global position for playback order and may additionally reference a section; the API exposes both the ordered `sections` list and each unit's `sectionId`.

The curriculum replacement endpoint accepts the complete section tree and the remaining unsectioned lesson IDs in one request. It validates ownership and uniqueness before changing anything, then applies section order, lesson membership, and global lesson positions in one database transaction. Omitting a section deletes only the section and preserves its lessons when the client places them in `unsectionedUnitIds`.

Flat published series remain valid. Once a published series uses sections, every lesson must belong to exactly one non-empty section. This invariant is enforced both when saving the curriculum and when adding or publishing content.

## 🎬 Static HLS

Set `STATIC_HLS_ENABLED=true`, configure `STATIC_HLS_BASE_URL`, and optionally restrict registrations with `STATIC_HLS_ALLOWED_PATH_PREFIX`. `POST /devs/api/v1/admin/media/static-hls` validates the master playlist, every rendition playlist, and every caption track before creating a ready media record. The service calculates duration by summing each rendition's `EXTINF` entries and rejects incomplete or materially mismatched renditions. Redirects and host-changing paths are rejected.

Caption files may already exist under the configured media origin, or an admin may use `POST /devs/api/v1/admin/media/caption-uploads` to receive a short-lived direct-to-R2 upload grant and then call `/caption-uploads/{uploadId}/complete`. The returned relative path is included in the static-HLS registration request. `PATCH /devs/api/v1/admin/media/{mediaId}/captions` replaces caption metadata without changing the immutable HLS package; the service validates each referenced WebVTT file before saving.

In production, `STATIC_HLS_ALLOWED_PATH_PREFIX` must match the dedicated key prefix used by Devs. Purge workers refuse prefix deletion when it is empty or when a manifest falls outside it. The R2 credential therefore needs bucket-scoped object read, write, and delete access; it does not need account-wide or bucket-creation permission.

The database stores relative paths, allowing the CDN hostname to change without rewriting catalog rows. See [the measured R2/HLS pilot](docs/r2-hls-pilot-results.md) and [the reproducible pilot guide](docs/r2-hls-pilot-guide.md).

## ⚙️ Configuration

| Variable | Purpose | Default |
|---|---|---|
| `DB_URL`, `DB_USERNAME`, `DB_PASSWORD` | PostgreSQL connection | Local `devs` database |
| `DEVS_FRONTEND_ORIGINS` | Comma-separated browser origins | `http://localhost:3000` |
| `DEVS_JWT_ENABLED` | Enable KStacks JWT validation | `false` |
| `DEVS_JWT_PUBLIC_KEY`, `DEVS_JWT_ISSUER` | ES256 trust configuration | Empty |
| `DEVS_ADMIN_ROLES`, `DEVS_STUDENT_ROLES` | Authorized central roles | `DEVS_ADMIN,ADMIN` / `STUDENT` |
| `STATIC_HLS_BASE_URL` | Public HLS and caption origin | Invalid placeholder |
| `STATIC_HLS_ALLOWED_PATH_PREFIX` | Allowed immutable HLS/caption key prefix | Empty |
| `R2_ENABLED` | Enable direct object-storage operations | `false` |
| `R2_ENDPOINT`, `R2_BUCKET` | R2 S3 endpoint and bucket | Empty |
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | Runtime-only R2 credentials | Empty |
| `ATTACHMENTS_PUBLIC_BASE_URL` | Public R2 custom-domain origin | HLS base URL |
| `ATTACHMENTS_MAX_UPLOAD_BYTES` | Per-file attachment limit | `104857600` |
| `ATTACHMENTS_MAX_PER_UNIT` | Active attachments per lesson | `20` |
| `ATTACHMENTS_RETENTION` | Soft-deletion retention | `P7D` |
| `ATTACHMENTS_PURGE_DELAY` | Cleanup scan interval | `PT1H` |
| `ATTACHMENTS_STALE_UPLOAD_AFTER` | Remove unconfirmed upload records and objects after | `PT24H` |
| `DEVS_MEDIA_RETENTION` | Deleted media and cover retention | `P7D` |
| `DEVS_MEDIA_VERSION_RETENTION` | Replaced lesson-video rollback window | `P30D` |
| `DEVS_MEDIA_PURGE_DELAY` | Media and cover cleanup scan interval | `PT1H` |
| `DEVS_MEDIA_STALE_UPLOAD_AFTER` | Remove unconfirmed caption uploads after | `PT24H` |
| `DEVS_TRASH_PURGE_DELAY` | Content and lesson trash cleanup scan interval | `PT1H` |

The complete reference is in [.env.example](.env.example) and [the architecture guide](docs/architecture.md). Never commit populated environment files or pass credentials as Docker build arguments.

## 🐳 Delivery

The production image runs as UID `10001`, includes an explicit health check, and supports both `linux/amd64` and `linux/arm64`. GitHub Actions verifies pull requests to `dev` and `prod`; pushes publish immutable branch/SHA tags to GHCR.

Following KStacks GitOps conventions, the final deployment handoff should update `ghcr.io/kstacks-org/devs-service:prod-<short-sha>` in the separate infrastructure repository. That automation is intentionally deferred until the Devs infra manifest and its narrowly scoped `INFRA_PAT` secret exist.

## 🗂️ Repository structure

- `src/main/java/`: content, attachment, media, and security modules
- `src/main/resources/db/migration/`: append-only Flyway migrations
- `src/test/`: unit and application-context tests
- `deploy/kubernetes/`: deployment base and operational notes
- `docs/`: architecture, planning, and video-pilot documentation
- `tools/telegram-import/`: resumable Telegram migration utility
- `tools/static-hls-import/`: resumable Drive-to-R2 HLS backlog importer

## 🤝 Contributing

Create work from `dev`, keep migrations append-only, run `mvn verify`, and open a pull request. Production releases merge through `prod`; credentials and environment-specific hosts belong in infrastructure-managed secrets and configuration.

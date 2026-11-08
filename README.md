# Store Pythoine 0.0.4

Store Pythoine is Suite Pythoine's optional headless Store backend. Suite owns the visible Store frontend, trusted component identity, installed state, App Pages and managed installation.

## 0.0.4 — trusted repository discovery MVP

0.0.4 removes the central online `store-content.json` as version authority.

On an explicit **Refresh Store Online** request, Suite publishes a local snapshot of the component IDs and GitHub repositories already authorised by Suite's trusted catalogue. Store checks only those repositories, reading each repository's root `suite-pythoine-component.json` (main, with master fallback), validates the component identity/publisher/repository, and records the version declared by that application itself.

The generated XDG `store-content.json` remains a normalized local/offline snapshot and rich-content contract consumed by Suite. It is not the remote source of truth for application versions.

Repository checks are bounded and concurrent. A failed or missing repository is isolated to that application and does not abort successful checks for other applications.

0.0.4 keeps the older verified-download backend code for compatibility, but repository update discovery does not require download URLs, hashes or package sizes. The MVP action presented by Suite is **View Release**.

Backend commands:

- `--publish-content`
- `--refresh-online`
- `--download-component <component-id>` (retained compatibility capability)
- `--version`

Normal Store browsing remains offline. Network activity occurs only when Suite explicitly requests an online refresh or retained acquisition operation.

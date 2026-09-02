# Project Agent Requirements

## Data Collection Safety

- Do not start a broad or historical data download unless the user explicitly requests that scope.
- Never rely on implicit date defaults for data collection. Every collection call must receive an explicit `start_date` and `end_date`.
- Scheduled daily collection must request only the current trading date or an explicitly identified missing trading date. It must not silently fall back to multi-year history.
- Keep external requests serial and rate-limited. Preserve the source adapter's configured interval, retry limit, exponential backoff, and checkpoint/resume behavior.
- Do not increase concurrency, remove throttling, or retry aggressively to speed up collection. Avoid source blocking and IP bans as a primary operational requirement.
- Before a large collection, report the symbol count, date range, estimated request count, interval, retry policy, and expected duration. Wait for explicit approval unless the user specifically requested the large collection.
- Prefer resuming from checkpoints and filling identified gaps over re-requesting completed history.
- Store collection results through the existing Raw Batch, quality, publish, and DatasetAccess pipeline. Do not write ad hoc production files from scripts or pages.
- A failed or empty response is not success. Record it as skipped or failed with details and keep it eligible for a later controlled retry.

## Data Integrity

- Do not delete or overwrite existing market data without explicit user confirmation and a stated recovery plan.
- Do not bypass Published Dataset, quality, checksum, or version checks for formal analysis and decisions.
- Keep source classifications and data dates explicit; do not silently merge incompatible industry or sector definitions.

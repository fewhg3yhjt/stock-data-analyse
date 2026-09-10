# Task Definitions

Task YAML files describe what a task is and how it is scheduled. Runtime facts
are written to SQLite; changing a YAML file does not become active until the
task configuration is explicitly activated.

For `stock_daily_capture`, the `execution` section also controls Raw capture
behavior:

- `raw_run_date`: Raw directory date; use `period_end` for the requested business date.
- `raw_subdir`: optional Raw child directory such as `_tmp`.
- `raw_batch_size`: maximum securities per Raw file.
- `auto_merge_effective`: whether capture also updates Current Raw.
- `pending_codes_path`: optional explicit CSV path used for a controlled missing-code retry.

These settings are stage controls, not implicit fallbacks. A pending-code
retry must still provide an explicit `period_start` and `period_end`; successful
codes are removed from the CSV and failed or empty responses remain eligible.

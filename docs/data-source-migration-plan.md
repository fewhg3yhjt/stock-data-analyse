# Data Source Migration Plan

## Target

All production business reads and writes must use the new data module:

```text
Raw Batch -> Candidate -> Quality -> Publish -> DatasetAccess
management.db -> dataset_current/dataset_versions/task state
```

The legacy `meta.db` and `warehouse.db` files are not valid production data
sources. They may remain as explicitly isolated test fixtures or migration
inputs, but production code must not silently fall back to them.

## Known Legacy References

- `warehouse/storage.py`: `Warehouse` still falls back to
  `output/data/warehouse/meta.db` when `MANAGEMENT_DB_PATH` is absent. Remove
  this production fallback after all entry points pass the active management
  database explicitly.
- `warehouse/collector.py`, `warehouse/fundamentals_collect.py`,
  `warehouse/cli.py`, and `strategy_lab.py`: comments and legacy collection
  paths still describe `meta.db` instrument metadata. Migrate these reads and
  writes to Published datasets and `DatasetAccess`.
- `warehouse/raw.py` and `warehouse/contract_diagnostics.py`: documentation
  still mentions `meta.db`; update the wording to distinguish migration
  fixtures from the active management database.
- `config/datasets/industry.yaml`: the partition declaration points at
  `warehouse/meta.db.instruments`; replace it with a Published dataset
  contract before enabling this dataset as a production consumer.
- `web/app.py`: legacy instrument/industry comments and any direct instrument
  table reads need to be replaced with DatasetAccess-backed data contracts.
- `ops/management_db.py`: the old-database migration helper remains an
  explicit migration tool only and must not be used as a runtime data source.

## Execution Order

1. Keep existing Published files and versions intact; do not overwrite or
   delete market data.
2. Register all YAML task definitions in the active `management.db`, including
   `industry_daily_capture` and `industry_features_build`.
3. Make industry K-line and rotation reads use only Published datasets.
4. Run the derived industry feature task for an explicit trading date and
   publish only PASS output through the normal pipeline.
5. Replace remaining runtime `meta.db`/`warehouse.db` reads, one subsystem at a
   time, with DatasetAccess or an explicit management database service.
6. Remove the implicit legacy fallback from production entry points after the
   test fixtures and migration tooling no longer depend on it.

## Verification

- Audit runtime references after each migration step.
- Confirm `dataset_current` and `dataset_versions` point to the expected
  Published files.
- Confirm each task has an active definition and a recorded run.
- Confirm web responses include explicit dataset category and `as_of` values.

# Dataset Definitions

Each YAML file under this directory is the declarative definition of one dataset.

- YAML defines schema, keys, sources, mappings, consumers, and quality policy.
- SQLite stores the runtime projection and generated execution facts.
- Raw Parquet/CSV files store immutable source evidence.

The definitions are loaded through `warehouse.dataset_config` and projected by
`warehouse.metadata.MetadataStore`; application code must not duplicate these
dataset definitions as Python constants.

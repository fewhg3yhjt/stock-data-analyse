from pathlib import Path

import yaml


def test_catalog_output_columns_match_dataset_fields_and_keys():
    root = Path(__file__).resolve().parents[1]
    catalog = yaml.safe_load((root / "config/metrics/catalog.yaml").read_text())['metrics']
    fields = {}
    for path in (root / "config/datasets").glob("*.yaml"):
        data = yaml.safe_load(path.read_text())
        fields[data["dataset"]["name"]] = {item["name"] for item in data["fields"]}
    for metric in catalog:
        if metric.get("output_column"):
            assert metric["output_column"] == metric["key"]
            assert metric["output_column"] in fields[metric["dataset"]]

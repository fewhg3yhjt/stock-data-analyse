import pandas as pd


def test_partial_rebuild_removes_only_selected_symbols():
    old = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-01", "2026-01-01"]),
        "code": ["sh600000", "sh600004"], "value": [1, 2],
    })
    new = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-01"]), "code": ["sh600000"], "value": [3],
    })
    selected = set(new["code"])
    merged = pd.concat([old[~old["code"].isin(selected)], new], ignore_index=True)
    assert merged.sort_values("code")["value"].tolist() == [3, 2]

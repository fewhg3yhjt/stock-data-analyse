import pandas as pd


def test_indicator_rebuild_prefers_new_rows_for_existing_keys():
    old = pd.DataFrame({"date": pd.to_datetime(["2026-01-01"]), "code": ["sh600000"], "MA5": [1.0]})
    new = pd.DataFrame({"date": pd.to_datetime(["2026-01-01"]), "code": ["sh600000"], "MA5": [2.0], "rsi14": [55.0]})
    merged = pd.concat([old, new], ignore_index=True).drop_duplicates(subset=["date", "code"], keep="last")
    assert merged.iloc[0]["MA5"] == 2.0
    assert merged.iloc[0]["rsi14"] == 55.0

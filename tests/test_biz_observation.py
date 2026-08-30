# -*- coding: utf-8 -*-
"""biz 包单元测试：Observation 状态机与服务。"""

import pytest

from StockInvestmentTool.biz.db import BusinessDB
from StockInvestmentTool.biz.observation import (
    OBS_ABANDONED,
    OBS_ARCHIVED,
    OBS_DISCOVERED,
    OBS_EXPIRED,
    OBS_OBSERVING,
    OBS_PAUSED,
    OBS_PROMOTED,
    OBS_READY_FOR_ENTRY,
    Observation,
    ObservationService,
    ObservationStateError,
)
from StockInvestmentTool.biz.repo import BusinessRepository


@pytest.fixture
def service(tmp_path):
    return ObservationService(BusinessRepository(BusinessDB(tmp_path / "obs.db")))


def make_obs(symbol="sh600908", status=OBS_DISCOVERED):
    return Observation(observation_id=f"obs_{symbol}", symbol=symbol, status=status)


class TestObservationService:
    def test_create_from_screen(self, service):
        obs = service.create_observation(
            "sh600908", source_type="screen", screen_run_id="run1",
            screen_candidate_id="cand1", reason_snapshot={"close": 11.8},
        )
        assert obs.status == OBS_DISCOVERED
        row = service.repo.db.fetchone("SELECT * FROM observations WHERE observation_id=?", (obs.observation_id,))
        assert row["symbol"] == "sh600908"
        link = service.repo.db.fetchone("SELECT * FROM observation_sources WHERE observation_id=?", (obs.observation_id,))
        assert link["source_type"] == "screen"
        ev = service.repo.db.fetchone("SELECT * FROM observation_events WHERE observation_id=?", (obs.observation_id,))
        assert ev["event_type"] == "CREATED"

    def test_transition_valid(self, service):
        obs = make_obs()
        event = service.transition(obs, OBS_OBSERVING)
        assert obs.status == OBS_OBSERVING
        assert event.from_status == OBS_DISCOVERED
        assert event.to_status == OBS_OBSERVING

    def test_transition_invalid_raises(self, service):
        obs = make_obs()
        with pytest.raises(ObservationStateError):
            service.transition(obs, OBS_PROMOTED)  # discovered 不能直接 promoted

    def test_full_flow(self, service):
        obs = make_obs()
        service.transition(obs, OBS_OBSERVING)
        service.transition(obs, OBS_READY_FOR_ENTRY)
        service.promote(obs, "cycle1")
        assert obs.status == OBS_PROMOTED
        assert obs.promoted_position_cycle_id == "cycle1"
        # promoted 只读
        with pytest.raises(ObservationStateError):
            service.transition(obs, OBS_OBSERVING)

    def test_archive(self, service):
        obs = make_obs()
        service.transition(obs, OBS_ABANDONED)
        service.transition(obs, OBS_ARCHIVED)
        assert obs.status == OBS_ARCHIVED

    def test_pause_resume(self, service):
        obs = make_obs(status=OBS_OBSERVING)
        service.pause(obs)
        assert obs.status == OBS_PAUSED
        service.resume(obs)
        assert obs.status == OBS_OBSERVING

    def test_expiry_reconcile(self, service):
        # 已过期的 observing 记录 → expired
        obs = make_obs(status=OBS_OBSERVING)
        obs.expires_at = "2026-08-01"
        obs2 = make_obs(status=OBS_OBSERVING)
        obs2.expires_at = "2026-12-31"  # 未过期
        events = service.reconcile_expiry([obs, obs2], now="2026-08-15T00:00:00Z")
        assert len(events) == 1
        assert obs.status == OBS_EXPIRED
        assert obs2.status == OBS_OBSERVING

    def test_expiry_not_forced_on_get(self, service):
        # 普通 GET 只返回 effective_status，不写库（此处验证 reconcile 是唯一推进者）
        obs = service.create_observation("sh601211")
        obs.expires_at = "2026-08-01"
        service.update_observation(obs)
        row = service.get_observation(obs.observation_id)
        assert row is not None
        assert row.status == OBS_DISCOVERED  # 未跑 reconcile 前不推进

    def test_persist_and_get(self, service):
        obs = service.create_observation("sz000001", name="平安银行")
        got = service.get_observation(obs.observation_id)
        assert got.symbol == "sz000001"
        assert got.status == OBS_DISCOVERED
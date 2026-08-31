# -*- coding: utf-8 -*-
"""Observation / WatchSubscription / DiscoveryLink 与状态机。

依据 docs/OBSERVATION_AND_WATCHLIST_DESIGN.md。
- 筛选候选、用户关注、观察周期三者分离
- 状态机 8 态，状态转换由服务层统一执行，页面不得直接改状态
- expiry 由 observation.expiry_reconcile 维护任务持久化推进
- 模拟执行状态属于 SimulationRun，Observation 不重复维护 simulating/simulated
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from StockInvestmentTool.biz.code import normalize
from StockInvestmentTool.biz.models import new_id, now_utc

logger = logging.getLogger(__name__)

# 状态
OBS_DISCOVERED = "discovered"
OBS_OBSERVING = "observing"
OBS_READY_FOR_ENTRY = "ready_for_entry"
OBS_PROMOTED = "promoted"
OBS_PAUSED = "paused"
OBS_EXPIRED = "expired"
OBS_ABANDONED = "abandoned"
OBS_ARCHIVED = "archived"

OBSERVATION_STATES = {
    OBS_DISCOVERED, OBS_OBSERVING, OBS_READY_FOR_ENTRY, OBS_PROMOTED,
    OBS_PAUSED, OBS_EXPIRED, OBS_ABANDONED, OBS_ARCHIVED,
}

# 合法转换
OBSERVATION_TRANSITIONS: dict[str, set[str]] = {
    OBS_DISCOVERED: {OBS_OBSERVING, OBS_ABANDONED, OBS_EXPIRED},
    OBS_OBSERVING: {OBS_READY_FOR_ENTRY, OBS_PAUSED, OBS_EXPIRED, OBS_ABANDONED},
    OBS_READY_FOR_ENTRY: {OBS_PROMOTED, OBS_OBSERVING, OBS_EXPIRED, OBS_ABANDONED},
    OBS_PAUSED: {OBS_OBSERVING, OBS_ABANDONED, OBS_ARCHIVED},
    OBS_EXPIRED: {OBS_OBSERVING, OBS_ARCHIVED},
    OBS_ABANDONED: {OBS_ARCHIVED},
    OBS_PROMOTED: {OBS_ARCHIVED},
    OBS_ARCHIVED: set(),
}

# 事件类型
EVENT_CREATED = "CREATED"
EVENT_SOURCE_LINKED = "SOURCE_LINKED"
EVENT_SUBSCRIPTION_CREATED = "SUBSCRIPTION_CREATED"
EVENT_SIMULATION_STARTED = "SIMULATION_STARTED"
EVENT_SIMULATION_COMPLETED = "SIMULATION_COMPLETED"
EVENT_SIMULATION_FAILED = "SIMULATION_FAILED"
EVENT_PAUSED = "PAUSED"
EVENT_RESUMED = "RESUMED"
EVENT_EXPIRED = "EXPIRED"
EVENT_PROMOTED = "PROMOTED_TO_POSITION"
EVENT_ABANDONED = "ABANDONED"
EVENT_ARCHIVED = "ARCHIVED"


@dataclass
class Observation:
    observation_id: str
    symbol: str
    status: str = OBS_DISCOVERED
    name: str = ""
    asset_type: str = "stock"
    current_strategy_version_id: str | None = None
    observation_reason: str = ""
    target_amount: float | None = None
    started_at: str = field(default_factory=now_utc)
    expires_at: str | None = None
    latest_data_as_of: str | None = None
    latest_simulation_run_id: str | None = None
    latest_research_run_id: str | None = None
    promoted_position_cycle_id: str | None = None
    created_at: str = field(default_factory=now_utc)
    updated_at: str = field(default_factory=now_utc)


@dataclass
class WatchSubscription:
    subscription_id: str
    symbol: str
    name: str = ""
    asset_type: str = "stock"
    purpose: str = "research"   # research/candidate/potential_entry/holding_mirror
    target_amount: float | None = None
    notes: str = ""
    status: str = "active"      # active/paused/ended
    started_at: str = field(default_factory=now_utc)
    paused_at: str | None = None
    ended_at: str | None = None
    created_at: str = field(default_factory=now_utc)
    updated_at: str = field(default_factory=now_utc)


@dataclass
class DiscoveryLink:
    link_id: str
    observation_id: str
    source_type: str = "screen"   # screen/strategy/money_flow/manual/holding/imported
    screen_run_id: str | None = None
    screen_candidate_id: str | None = None
    source_strategy_version_id: str | None = None
    discovered_at: str = field(default_factory=now_utc)
    reason_snapshot: dict = field(default_factory=dict)
    data_as_of: str | None = None
    created_at: str = field(default_factory=now_utc)


@dataclass
class ObservationEvent:
    event_id: str
    observation_id: str
    event_type: str
    event_time: str = field(default_factory=now_utc)
    from_status: str = ""
    to_status: str = ""
    source_id: str = ""
    reason: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class ObservationSnapshot:
    snapshot_id: str
    observation_id: str
    snapshot_time: str
    data_as_of: str
    strategy_version_id: str | None = None
    price: float | None = None
    market_regime: dict = field(default_factory=dict)
    entry_plan: dict = field(default_factory=dict)
    stop_plan: dict = field(default_factory=dict)
    decision_action: str = ""
    decision_reason: str = ""
    data_context: dict = field(default_factory=dict)


class ObservationStateError(ValueError):
    """非法状态转换。"""


class ObservationService:
    """观察池服务：状态转换唯一入口 + 事件记录 + expiry 推进。"""

    def __init__(self, repo=None):
        from StockInvestmentTool.biz.repo import BusinessRepository
        self.repo = repo or BusinessRepository()

    # ── 状态转换 ──────────────────────────────────────────

    def transition(self, obs: Observation, to_status: str, *, reason: str = "",
                   source_id: str = "") -> ObservationEvent:
        """执行合法状态转换并记录事件。非法转换抛 ObservationStateError。"""
        allowed = OBSERVATION_TRANSITIONS.get(obs.status, set())
        if to_status not in allowed:
            raise ObservationStateError(
                f"非法状态转换: {obs.status} -> {to_status}")
        from_status = obs.status
        obs.status = to_status
        obs.updated_at = now_utc()
        event = ObservationEvent(
            event_id=new_id("oe"),
            observation_id=obs.observation_id,
            event_type=_event_for_transition(from_status, to_status),
            from_status=from_status, to_status=to_status,
            source_id=source_id, reason=reason,
        )
        return event

    def transition_and_save(self, obs: Observation, to_status: str, *, reason: str = "",
                            source_id: str = "") -> ObservationEvent:
        """执行状态转换并立即持久化状态与事件。"""
        event = self.transition(obs, to_status, reason=reason, source_id=source_id)
        self._persist_transition(obs, event)
        return event

    def create_observation(self, symbol: str, *, name: str = "",
                           reason: str = "", target_amount: float | None = None,
                           expires_at: str | None = None,
                           source_type: str = "manual",
                           screen_run_id: str | None = None,
                           screen_candidate_id: str | None = None,
                           strategy_version_id: str | None = None,
                           data_as_of: str | None = None,
                           reason_snapshot: dict | None = None) -> Observation:
        """从候选或手工创建观察对象，并建立 DiscoveryLink。"""
        obs = Observation(
            observation_id=new_id("obs"),
            symbol=normalize(symbol), name=name, status=OBS_DISCOVERED,
            observation_reason=reason, target_amount=target_amount,
            expires_at=expires_at,
        )
        event = ObservationEvent(
            event_id=new_id("oe"), observation_id=obs.observation_id,
            event_type=EVENT_CREATED, from_status="", to_status=OBS_DISCOVERED,
            reason=reason,
        )
        link = None
        if source_type in {"screen", "strategy", "money_flow"}:
            link = DiscoveryLink(
                link_id=new_id("lk"), observation_id=obs.observation_id,
                source_type=source_type, screen_run_id=screen_run_id,
                screen_candidate_id=screen_candidate_id,
                source_strategy_version_id=strategy_version_id,
                data_as_of=data_as_of, reason_snapshot=reason_snapshot or {},
            )
        return self._persist(obs, event, link)

    def set_ready_for_entry(self, obs: Observation, *, source_id: str = "") -> ObservationEvent:
        return self.transition(obs, OBS_READY_FOR_ENTRY,
                               reason="用户确认可进入建仓", source_id=source_id)

    def promote(self, obs: Observation, position_cycle_id: str) -> ObservationEvent:
        """真实成交后回写：Observation → promoted。"""
        event = self.transition(obs, OBS_PROMOTED, reason="真实建仓已成交")
        obs.promoted_position_cycle_id = position_cycle_id
        obs.updated_at = now_utc()
        return event

    def promote_and_save(self, obs: Observation, position_cycle_id: str) -> ObservationEvent:
        event = self.promote(obs, position_cycle_id)
        self._persist_transition(obs, event)
        return event

    def _persist_transition(self, obs: Observation, event: ObservationEvent) -> None:
        """在同一事务中保存状态字段和状态事件。"""
        from StockInvestmentTool.biz.db import dumps_json

        with self.repo.db.transaction() as conn:
            conn.execute(
                "UPDATE observations SET status=?, promoted_position_cycle_id=?, "
                "updated_at=? WHERE observation_id=?",
                (obs.status, obs.promoted_position_cycle_id or "", now_utc(), obs.observation_id),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise KeyError(f"unknown observation: {obs.observation_id}")
            conn.execute(
                "INSERT INTO observation_events "
                "(event_id,observation_id,event_type,event_time,from_status,to_status,source_id,reason,metadata_json) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (event.event_id, event.observation_id, event.event_type, event.event_time,
                 event.from_status, event.to_status, event.source_id, event.reason,
                 dumps_json(event.metadata)),
            )

    def pause(self, obs: Observation) -> ObservationEvent:
        return self.transition(obs, OBS_PAUSED, reason="用户暂停观察")

    def resume(self, obs: Observation) -> ObservationEvent:
        return self.transition(obs, OBS_OBSERVING, reason="用户恢复观察")

    def abandon(self, obs: Observation) -> ObservationEvent:
        return self.transition(obs, OBS_ABANDONED, reason="用户放弃")

    def archive(self, obs: Observation) -> ObservationEvent:
        return self.transition(obs, OBS_ARCHIVED, reason="归档")

    # ── expiry 推进 ───────────────────────────────────────

    def reconcile_expiry(self, active_observations: list[Observation],
                         now: str | None = None) -> list[ObservationEvent]:
        """observation.expiry_reconcile：过期维护任务。

        对 observing/ready_for_entry 且 expires_at 已到的记录推进到 expired。
        now 为 UTC 时间字符串（YYYY-MM-DDTHH:MM:SSZ）；expires_at 若为纯日期则比较日期。
        """
        now = now or now_utc()
        events: list[ObservationEvent] = []
        for obs in active_observations:
            if obs.status not in {OBS_OBSERVING, OBS_READY_FOR_ENTRY}:
                continue
            if not obs.expires_at:
                continue
            if self._is_expired(obs.expires_at, now):
                events.append(self.transition(obs, OBS_EXPIRED, reason="观察有效期结束"))
        return events

    def reconcile_expiry_and_save(self, now: str | None = None) -> list[ObservationEvent]:
        """查询并持久化所有到期 Observation，供维护任务使用。"""
        observations = self.list_active_observations()
        events = self.reconcile_expiry(observations, now=now)
        for obs, event in zip(
            (item for item in observations if item.status == OBS_EXPIRED), events
        ):
            self._persist_transition(obs, event)
        return events

    @staticmethod
    def _is_expired(expires_at: str, now: str) -> bool:
        # 支持 YYYY-MM-DD 与 YYYY-MM-DDTHH:MM:SSZ 两种格式
        e = expires_at[:10]
        n = now[:10]
        return n > e

    # ── 订阅 ──────────────────────────────────────────────

    def create_subscription(self, symbol: str, *, name: str = "",
                            purpose: str = "research", notes: str = "",
                            target_amount: float | None = None) -> WatchSubscription:
        return WatchSubscription(
            subscription_id=new_id("sub"), symbol=normalize(symbol), name=name,
            purpose=purpose, notes=notes, target_amount=target_amount,
        )

    def save_subscription(self, subscription: WatchSubscription) -> WatchSubscription:
        """保存用户关注关系；结束关系不删除历史观察数据。"""
        self.repo.db.upsert("watch_subscriptions", {
            "subscription_id": subscription.subscription_id,
            "symbol": normalize(subscription.symbol), "name": subscription.name,
            "asset_type": subscription.asset_type, "purpose": subscription.purpose,
            "target_amount": subscription.target_amount, "notes": subscription.notes,
            "status": subscription.status, "started_at": subscription.started_at,
            "paused_at": subscription.paused_at or "", "ended_at": subscription.ended_at or "",
            "created_at": subscription.created_at, "updated_at": now_utc(),
        }, "subscription_id")
        return subscription

    def list_observations(self, status: str | None = None) -> list[Observation]:
        if status:
            rows = self.repo.db.fetchall(
                "SELECT * FROM observations WHERE status=? ORDER BY updated_at DESC", (status,)
            )
        else:
            rows = self.repo.db.fetchall("SELECT * FROM observations ORDER BY updated_at DESC")
        return [Observation(**self._row_to_obs_dict(row)) for row in rows]

    def save_snapshot(self, snapshot: ObservationSnapshot) -> ObservationSnapshot:
        from StockInvestmentTool.biz.db import dumps_json

        self.repo.db.insert("observation_snapshots", {
            "snapshot_id": snapshot.snapshot_id,
            "observation_id": snapshot.observation_id,
            "snapshot_time": snapshot.snapshot_time,
            "data_as_of": snapshot.data_as_of,
            "strategy_version_id": snapshot.strategy_version_id or "",
            "price": snapshot.price,
            "market_regime_json": dumps_json(snapshot.market_regime),
            "entry_plan_json": dumps_json(snapshot.entry_plan),
            "stop_plan_json": dumps_json(snapshot.stop_plan),
            "decision_action": snapshot.decision_action,
            "decision_reason": snapshot.decision_reason,
            "data_context_json": dumps_json(snapshot.data_context),
            "created_at": now_utc(),
        })
        return snapshot

    # ── 持久化 ────────────────────────────────────────────

    def _persist(self, obs: Observation, event: ObservationEvent,
                 link: DiscoveryLink | None) -> Observation:
        from StockInvestmentTool.biz.db import dumps_json, now_utc

        with self.repo.db.transaction() as conn:
            conn.execute("""
                INSERT INTO observations
                (observation_id,symbol,name,asset_type,status,current_strategy_version_id,
                 observation_reason,target_amount,started_at,expires_at,latest_data_as_of,
                 latest_simulation_run_id,latest_research_run_id,promoted_position_cycle_id,
                 created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
            obs.observation_id, obs.symbol, obs.name, obs.asset_type, obs.status,
            obs.current_strategy_version_id or "", obs.observation_reason,
            obs.target_amount, obs.started_at, obs.expires_at or "",
            obs.latest_data_as_of or "", obs.latest_simulation_run_id or "",
            obs.latest_research_run_id or "", obs.promoted_position_cycle_id or "",
            obs.created_at, obs.updated_at,
            ))
            conn.execute("""
                INSERT INTO observation_events
                (event_id,observation_id,event_type,event_time,from_status,to_status,
                 source_id,reason,metadata_json)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (
            event.event_id, event.observation_id, event.event_type, event.event_time,
            event.from_status, event.to_status, event.source_id, event.reason,
            dumps_json(event.metadata),
            ))
            if link is not None:
                conn.execute("""
                    INSERT INTO observation_sources
                    (link_id,observation_id,source_type,screen_run_id,screen_candidate_id,
                     source_strategy_version_id,discovered_at,reason_snapshot_json,data_as_of,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                """, (
                link.link_id, link.observation_id, link.source_type,
                link.screen_run_id or "", link.screen_candidate_id or "",
                link.source_strategy_version_id or "", link.discovered_at,
                dumps_json(link.reason_snapshot), link.data_as_of or "", link.created_at,
                ))
        return obs

    def save_event(self, obs_id: str, event: ObservationEvent) -> str:
        from StockInvestmentTool.biz.db import dumps_json
        self.repo.db.insert("observation_events", {
            "event_id": event.event_id, "observation_id": obs_id,
            "event_type": event.event_type, "event_time": event.event_time,
            "from_status": event.from_status, "to_status": event.to_status,
            "source_id": event.source_id, "reason": event.reason,
            "metadata_json": dumps_json(event.metadata),
        })
        return event.event_id

    def update_observation(self, obs: Observation) -> None:
        self.repo.db.update("observations", {
            "status": obs.status, "name": obs.name,
            "current_strategy_version_id": obs.current_strategy_version_id or "",
            "latest_data_as_of": obs.latest_data_as_of or "",
            "latest_simulation_run_id": obs.latest_simulation_run_id or "",
            "latest_research_run_id": obs.latest_research_run_id or "",
            "promoted_position_cycle_id": obs.promoted_position_cycle_id or "",
            "expires_at": obs.expires_at or "",
            "updated_at": now_utc(),
        }, "observation_id=?", (obs.observation_id,))

    def get_observation(self, observation_id: str) -> Observation | None:
        row = self.repo.db.fetchone(
            "SELECT * FROM observations WHERE observation_id=?", (observation_id,))
        if not row:
            return None
        return Observation(
            observation_id=row["observation_id"], symbol=row["symbol"],
            name=row["name"], asset_type=row["asset_type"], status=row["status"],
            current_strategy_version_id=row["current_strategy_version_id"] or None,
            observation_reason=row["observation_reason"],
            target_amount=row["target_amount"],
            started_at=row["started_at"], expires_at=row["expires_at"] or None,
            latest_data_as_of=row["latest_data_as_of"] or None,
            latest_simulation_run_id=row["latest_simulation_run_id"] or None,
            latest_research_run_id=row["latest_research_run_id"] or None,
            promoted_position_cycle_id=row["promoted_position_cycle_id"] or None,
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def list_active_observations(self) -> list[Observation]:
        rows = self.repo.db.fetchall(
            "SELECT * FROM observations WHERE status IN ('discovered','observing','ready_for_entry') ORDER BY updated_at")
        return [Observation(**self._row_to_obs_dict(r)) for r in rows]

    @staticmethod
    def _row_to_obs_dict(row) -> dict:
        return {
            "observation_id": row["observation_id"], "symbol": row["symbol"],
            "name": row["name"], "asset_type": row["asset_type"], "status": row["status"],
            "current_strategy_version_id": row["current_strategy_version_id"] or None,
            "observation_reason": row["observation_reason"],
            "target_amount": row["target_amount"],
            "started_at": row["started_at"], "expires_at": row["expires_at"] or None,
            "latest_data_as_of": row["latest_data_as_of"] or None,
            "latest_simulation_run_id": row["latest_simulation_run_id"] or None,
            "latest_research_run_id": row["latest_research_run_id"] or None,
            "promoted_position_cycle_id": row["promoted_position_cycle_id"] or None,
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }


def _event_for_transition(from_status: str, to_status: str) -> str:
    mapping = {
        (OBS_DISCOVERED, OBS_OBSERVING): EVENT_RESUMED,
        (OBS_DISCOVERED, OBS_ABANDONED): EVENT_ABANDONED,
        (OBS_DISCOVERED, OBS_EXPIRED): EVENT_EXPIRED,
        (OBS_OBSERVING, OBS_READY_FOR_ENTRY): "READY_FOR_ENTRY",
        (OBS_OBSERVING, OBS_PAUSED): EVENT_PAUSED,
        (OBS_OBSERVING, OBS_EXPIRED): EVENT_EXPIRED,
        (OBS_OBSERVING, OBS_ABANDONED): EVENT_ABANDONED,
        (OBS_READY_FOR_ENTRY, OBS_PROMOTED): EVENT_PROMOTED,
        (OBS_READY_FOR_ENTRY, OBS_OBSERVING): "REVIEW_OBSERVING",
        (OBS_READY_FOR_ENTRY, OBS_EXPIRED): EVENT_EXPIRED,
        (OBS_READY_FOR_ENTRY, OBS_ABANDONED): EVENT_ABANDONED,
        (OBS_PAUSED, OBS_OBSERVING): EVENT_RESUMED,
        (OBS_PAUSED, OBS_ABANDONED): EVENT_ABANDONED,
        (OBS_PAUSED, OBS_ARCHIVED): EVENT_ARCHIVED,
        (OBS_EXPIRED, OBS_OBSERVING): EVENT_RESUMED,
        (OBS_EXPIRED, OBS_ARCHIVED): EVENT_ARCHIVED,
        (OBS_ABANDONED, OBS_ARCHIVED): EVENT_ARCHIVED,
        (OBS_PROMOTED, OBS_ARCHIVED): EVENT_ARCHIVED,
    }
    return mapping.get((from_status, to_status), "STATE_CHANGED")

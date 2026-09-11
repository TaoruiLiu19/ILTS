"""
船舶动态 —— 抽象留口，只做实「手动」（优化方案 D1 §2.3）

联网 / 实时 AIS 明确不做：纯本地离线，船位由操作员人工登记。
本模块保留 Provider 抽象，未来如需接 AIS，新增 AisProvider 并按优先级串联即可。

分层：UI → services（本模块）→ db
"""

from abc import ABC, abstractmethod


class VesselStatusProvider(ABC):
    """船位动态提供方抽象"""

    @abstractmethod
    def fetch(self, vessel):
        """
        返回动态 dict（可 None）：
            {lat, lon, actual_eta, note, created_at, source}
        """
        raise NotImplementedError


class ManualProvider(VesselStatusProvider):
    """离线实现：返回该项目最近一次人工登记的船位/实际 ETA"""

    source = "manual"

    def fetch(self, vessel):
        import db
        if not vessel:
            return None
        # vessel 为批次级记录：project_id 可能不在行内，按 batch_id 反解（§6.7）
        project_id = vessel.get("project_id")
        batch_id = vessel.get("batch_id")
        if not project_id and batch_id:
            b = db.get_batch(batch_id)
            project_id = b["project_id"] if b else None
        if not project_id:
            return None
        rows = db.get_vessel_positions(project_id, limit=1, batch_id=batch_id)
        if not rows:
            return None
        pos = rows[0]
        return {
            "lat": pos.get("lat"),
            "lon": pos.get("lon"),
            "actual_eta": pos.get("actual_eta"),
            "note": pos.get("note"),
            "created_at": pos.get("created_at"),
            "source": self.source,
        }


# 现状只启用 ManualProvider；未来如需 AIS，再实现 AisProvider 并按优先级串联
PROVIDERS = [ManualProvider()]


def fetch_latest_status(vessel):
    """按 PROVIDERS 顺序取第一个可用结果"""
    for provider in PROVIDERS:
        result = provider.fetch(vessel)
        if result:
            return result
    return None


def register_manual_position(project_id, lat=None, lon=None,
                             actual_eta=None, note=None, batch_id=None):
    """操作员人工登记一条船位动态（写入本地 vessel_positions 以便回放）"""
    import db
    db.insert_vessel_position(project_id, lat=lat, lon=lon,
                              actual_eta=actual_eta, note=note, batch_id=batch_id)
    return db.get_vessel(project_id, batch_id=batch_id)

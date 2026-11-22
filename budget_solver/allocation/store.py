"""代拨产物的文件存储与稳定单号。

每类产物（去向册/点名册/拨款账簿）各自维护一个目录与序号；产物整体带内容哈希。
稳定单号语义：

* 同号同内容：原样复读（幂等），不重复下账；
* 同号异文：抛 :class:`OrderRejectedError` 退回；
* 另一种测算不占款：走 dry-run，不占序号、不动已支余额。
"""

import os

from ..errors import OrderRejectedError
from ..util import content_hash, ensure_dir, read_json, utc_now_iso, write_json_atomic

# 各类产物自带的指纹/哈希字段，计算内容指纹时一律排除，避免把哈希自身算进指纹
_HASH_KEYS = ("content_hash", "ledger_fingerprint", "rollcall_fingerprint",
              "book_fingerprint", "order_fingerprint")


def fingerprint(payload):
    """代拨产物的稳定内容指纹：排除全部哈希字段后做规范哈希。"""
    return content_hash({k: v for k, v in payload.items() if k not in _HASH_KEYS})


class ArtifactStore:
    def __init__(self, root, kind, id_prefix, fingerprint_field=None):
        self.root = os.path.abspath(root)
        self.kind = kind
        self.id_prefix = id_prefix
        # 业务指纹字段（如 ledger_fingerprint），与通用 content_hash 同值，
        # 在全部字段就位后统一回填，保证重开可独立复算
        self.fingerprint_field = fingerprint_field
        ensure_dir(self.root)
        self._seq_path = os.path.join(self.root, "seq.json")

    def _path(self, artifact_id):
        return os.path.join(self.root, f"{artifact_id}.json")

    def next_id(self):
        seq = read_json(self._seq_path) if os.path.exists(self._seq_path) else {"next": 1}
        artifact_id = f"{self.id_prefix}{seq['next']}"
        seq["next"] += 1
        write_json_atomic(self._seq_path, seq)
        return artifact_id

    def save(self, artifact_id, payload):
        payload = dict(payload)
        payload.setdefault("created_at", utc_now_iso())
        # 单号字段按产物种类注入（ledger_id/roll_id/book_id/order_id）
        id_field = f"{self.id_prefix.rstrip('0123456789')}_id"
        payload.setdefault(id_field, artifact_id)
        # 重算前只剔除通用哈希；业务指纹（若声明）保留并在随后回填同值
        payload.pop("content_hash", None)
        digest = fingerprint(payload)
        payload["content_hash"] = digest
        if self.fingerprint_field:
            payload[self.fingerprint_field] = digest
        path = self._path(artifact_id)
        if os.path.exists(path):
            existing = read_json(path)
            if existing.get("content_hash") != payload["content_hash"]:
                raise OrderRejectedError(
                    f"稳定单号 {artifact_id} 已绑定不同内容（同号异文退回）",
                    [f"existing={existing.get('content_hash')}",
                     f"submitted={payload['content_hash']}"],
                )
            return existing  # 同号同内容：幂等复读
        write_json_atomic(path, payload)
        return payload

    def load(self, artifact_id):
        path = self._path(artifact_id)
        if not os.path.exists(path):
            from ..errors import NotFoundError

            raise NotFoundError(f"{self.kind} {artifact_id} 不存在")
        return read_json(path)

    def rewrite(self, artifact_id, payload):
        """可变产物（拨款账簿）追加订单后的回写：重算通用哈希并原子落盘。

        仅账簿使用；拨款单/去向册/点名册一经发布不可变，只能走 :meth:`save`
        的同号同内容/同号异文保护。
        """
        payload = dict(payload)
        payload.setdefault("created_at", utc_now_iso())
        id_field = f"{self.id_prefix.rstrip('0123456789')}_id"
        payload.setdefault(id_field, artifact_id)
        payload.pop("content_hash", None)
        payload["content_hash"] = fingerprint(payload)
        write_json_atomic(self._path(artifact_id), payload)
        return payload

    def list_ids(self):
        if not os.path.isdir(self.root):
            return []
        ids = [f[:-5] for f in os.listdir(self.root)
               if f.startswith(self.id_prefix) and f.endswith(".json")]

        def sort_key(value):
            suffix = value[len(self.id_prefix):]
            # 自动发号是纯整数后缀；显式稳定单号可能含非数字，按数字优先、文本兜底
            try:
                return (0, int(suffix))
            except ValueError:
                return (1, suffix)

        return sorted(ids, key=sort_key)

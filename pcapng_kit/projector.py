"""播放台：译码底/墙面状态机 + 最低损失排片搜索。

每个整数拍刻发生一个动作，片单必须按序逐帧推进，散场刻前全部完成。
动作集合（每拍恰好一种）：

* ``ORIG``    原放当前帧：整片无需底；差片要求译码底正是头中点名的帧，
              且它整条依赖链都没有被判冲突。正常显示后该帧成为新底。
* ``PRINT``   用排练期印好的实物画片放当前帧（占一拍，置为新底）。
* ``REPLACE`` 花该帧替放损失重放上一拍真正上墙的画面；第一帧用馆徽。
              译码底回到实际显示的那帧；馆徽不充当任何底。
* ``WAIT``    空等一拍，墙面与译码底都不变，不推进片单（等迟到证据）。
* ``LOAD``    用完整一拍把当时已确认的一张整片现场装为译码底；墙面不动、
              片单不推进。开拍前不免费预装底。

搜索为单次深度优先，状态含剩余印片张数/字节，按“同状态更低损失者支配”
做记忆化；WAIT 只在确有迟到证据可等时展开，LOAD 只在确有未来差片点名
该整片时展开，因此规模上限定在 20 帧、40 拍内可快速穷尽。

优化顺序：总损失最小 → 散场刻最早 → 所印总字节最少。物理上来不及则
返回不可行并给出阻断帧。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional

from . import frames as sf
from .album import FrameAlbum, FrameStatus

ACTION_ORIG = "orig"
ACTION_PRINT = "print"
ACTION_REPLACE = "replace"
ACTION_WAIT = "wait"
ACTION_LOAD = "load"

WALL_LOGO = "LOGO"          # 馆徽：开场墙面，不是任何帧，也不充当底片
BASE_NONE = None
MAX_PRINT_CARDS = 4
MAX_FRAMES = 20
MAX_BEATS = 40


@dataclass(frozen=True, slots=True)
class Beat:
    """一拍的可复读安排；馆员可据此核出墙面与译码底。"""

    tick: int
    action: str
    list_index: int | None                 # 涉及的片单位置（WAIT/LOAD 不推进）
    frame_no: object                       # int 帧号，或 WALL_LOGO
    loss: int
    wall_before: object
    base_before: object
    wall_after: object
    base_after: object
    note: str

    def as_dict(self) -> dict:
        return {
            "tick": self.tick,
            "action": self.action,
            "list_index": self.list_index,
            "frame": self.frame_no,
            "loss": self.loss,
            "wall_before": self.wall_before,
            "base_before": self.base_before,
            "wall_after": self.wall_after,
            "base_after": self.base_after,
            "note": self.note,
        }


@dataclass(slots=True)
class ShowPlan:
    feasible: bool
    total_loss: Optional[int]
    end_tick: Optional[int]
    printed_bytes: int
    printed_frames: list[int]
    beats: list[Beat]
    reason: str
    evidence_used: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "feasible": self.feasible,
            "total_loss": self.total_loss,
            "end_tick": self.end_tick,
            "printed_bytes": self.printed_bytes,
            "printed_frames": list(self.printed_frames),
            "reason": self.reason,
            "beats": [beat.as_dict() for beat in self.beats],
            "evidence_used": self.evidence_used,
        }


class ProjectorPlanner:
    """对给定收集册、片单、散场刻与印片额度求最优排片。"""

    def __init__(
        self,
        album: FrameAlbum,
        playlist: list[int],
        replace_loss: dict[int, int],
        curtain: int,
        *,
        print_byte_budget: int = 0,
        rehearsal_deadline: Fraction = Fraction(0),
        max_beats: int = MAX_BEATS,
    ) -> None:
        if len(playlist) > MAX_FRAMES:
            raise ValueError(f"at most {MAX_FRAMES} frames per show")
        if curtain > max_beats:
            raise ValueError(f"curtain {curtain} exceeds max beats {max_beats}")
        self.album = album
        self.playlist = list(playlist)
        self.replace_loss = dict(replace_loss)
        self.curtain = curtain
        self.print_byte_budget = print_byte_budget
        self.deadline = rehearsal_deadline
        self.max_beats = max_beats

        # 排练截止刻可印的帧及其画面字节数；开拍后再按当时证据复核链。
        self._printable: dict[int, int] = {}
        for frame_no in album.printable_frames(rehearsal_deadline):
            if frame_no in self.playlist:
                view = album.view_at(frame_no, rehearsal_deadline)
                self._printable[frame_no] = view.declared_len
        self._best: tuple | None = None       # (loss, end, used_bytes, beats)

    # ------------------------------------------------------------------
    def plan(self) -> ShowPlan:
        memo: dict[tuple, int] = {}
        beats: list[Beat] = []
        self._best = None
        self._dfs(
            tick=0, pos=0, wall=WALL_LOGO, base=BASE_NONE,
            cards_left=MAX_PRINT_CARDS, bytes_left=self.print_byte_budget,
            loss=0, memo=memo, beats=beats,
        )
        if self._best is None:
            blocker = self._physical_blocker()
            return ShowPlan(
                feasible=False,
                total_loss=None,
                end_tick=None,
                printed_bytes=0,
                printed_frames=[],
                beats=[],
                reason=(
                    "physically impossible to finish by curtain: "
                    f"first blocking frame {blocker[0]} ({blocker[1]})"
                ),
            )
        loss, end_tick, used_bytes, plan_beats = self._best
        printed = [
            beat.frame_no for beat in plan_beats if beat.action == ACTION_PRINT
        ]
        evidence = self.album.card_provenance(set(printed), self.deadline)
        return ShowPlan(
            feasible=True,
            total_loss=loss,
            end_tick=end_tick,
            printed_bytes=used_bytes,
            printed_frames=printed,
            beats=plan_beats,
            reason="optimal schedule (loss, then earliest curtain, then fewest print bytes)",
            evidence_used={str(k): v for k, v in evidence.items()},
        )

    # ------------------------------------------------------------------
    def _dfs(
        self,
        tick: int,
        pos: int,
        wall: object,
        base: object,
        cards_left: int,
        bytes_left: int,
        loss: int,
        memo: dict[tuple, int],
        beats: list[Beat],
    ) -> None:
        if pos == len(self.playlist):
            used_bytes = self.print_byte_budget - bytes_left
            key = (loss, tick, used_bytes)
            if self._best is None or key < self._best[0:3]:
                self._best = (loss, tick, used_bytes, list(beats))
            return
        if tick >= self.curtain:
            return                          # 散场仍未完成：该枝不可行
        remaining = len(self.playlist) - pos
        if tick + remaining > self.curtain:
            return                          # 每帧至少一拍，物理上来不及

        # 支配性记忆化：同一（拍刻, 片单位置, 墙, 底, 剩余印片资源）状态，
        # 先前以不高于当前的损失到达过，则后续可选动作完全相同、当前枝
        # 不可能更优，直接剪枝。
        state = (tick, pos, wall, base, cards_left, bytes_left)
        prior_loss = memo.get(state)
        if prior_loss is not None and prior_loss <= loss:
            return
        memo[state] = loss
        if self._best is not None and loss > self._best[0]:
            return                          # 损失已超最优界

        frame_no = self.playlist[pos]
        at = Fraction(tick)
        view = self.album.view_at(frame_no, at)
        branches: list[tuple] = []

        # 1) 原放（零损失，优先建立最优界）。
        if view.status is FrameStatus.COMPLETE:
            rendered = self.album.chain_check(frame_no, at)
            if rendered.ok and self._orig_base_ok(view, base, at):
                branches.append((0, ACTION_ORIG, None))

        # 2) 实物画片（零损失）：排练期可印且此刻证据链仍无冲突。
        card_size = self._printable.get(frame_no)
        if (
            card_size is not None
            and cards_left > 0
            and bytes_left >= card_size
            and self.album.chain_check(frame_no, at).ok
        ):
            branches.append((0, ACTION_PRINT, card_size))

        # 3) 替放（承担该帧损失）。
        branches.append((self.replace_loss[frame_no], ACTION_REPLACE, None))

        for branch_loss, action, extra in branches:
            if action == ACTION_ORIG:
                beats.append(self._beat(
                    tick, action, pos, frame_no, 0,
                    wall, base, frame_no, frame_no, self._orig_note(view, base),
                ))
                self._dfs(tick + 1, pos + 1, frame_no, frame_no,
                          cards_left, bytes_left, loss, memo, beats)
                beats.pop()
            elif action == ACTION_PRINT:
                beats.append(self._beat(
                    tick, action, pos, frame_no, 0,
                    wall, base, frame_no, frame_no,
                    "printed card projected; frame becomes new base",
                ))
                self._dfs(tick + 1, pos + 1, frame_no, frame_no,
                          cards_left - 1, bytes_left - extra, loss, memo, beats)
                beats.pop()
            else:
                new_base = BASE_NONE if wall == WALL_LOGO else wall
                beats.append(self._beat(
                    tick, action, pos, frame_no, branch_loss,
                    wall, base, wall, new_base,
                    "logo shown" if wall == WALL_LOGO else f"repeats frame {wall}",
                ))
                self._dfs(tick + 1, pos + 1, wall, new_base,
                          cards_left, bytes_left, loss + branch_loss, memo, beats)
                beats.pop()

        # 4) 空等：只在确有迟到证据、且等待后仍来得及完成时展开。
        if self._waiting_can_help(tick, pos) and tick + 1 + remaining <= self.curtain:
            beats.append(self._beat(
                tick, ACTION_WAIT, None, None, 0,
                wall, base, wall, base,
                "idle beat; wall and decoding base unchanged",
            ))
            self._dfs(tick + 1, pos, wall, base,
                      cards_left, bytes_left, loss, memo, beats)
            beats.pop()

        # 5) 现场装底：只装“未来某差片直接点名、且与当前底不同”的已确认整片。
        for full_no in self._loadable_full_frames(at, pos, base):
            beats.append(self._beat(
                tick, ACTION_LOAD, None, full_no, 0,
                wall, base, wall, full_no,
                f"confirmed full frame {full_no} loaded as decoding base",
            ))
            self._dfs(tick + 1, pos, wall, full_no,
                      cards_left, bytes_left, loss, memo, beats)
            beats.pop()

    # ------------------------------------------------------------------
    def _beat(
        self, tick: int, action: str, list_index: int | None,
        frame_no: object, loss: int,
        wall_before: object, base_before: object,
        wall_after: object, base_after: object, note: str,
    ) -> Beat:
        return Beat(
            tick=tick,
            action=action,
            list_index=list_index,
            frame_no=frame_no,
            loss=loss,
            wall_before=wall_before,
            base_before=base_before,
            wall_after=wall_after,
            base_after=base_after,
            note=note,
        )

    def _orig_base_ok(self, view, base: object, at: Fraction) -> bool:
        if view.kind == sf.KIND_FULL:
            return True
        if base != view.base_no:
            return False
        # 原放差片不能沿已判冲突的依赖链继续放。
        return self.album.chain_check(view.base_no, at).ok

    def _orig_note(self, view, base: object) -> str:
        if view.kind == sf.KIND_FULL:
            return "full frame needs no base"
        return f"delta frame decoded with required base {base}"

    def _waiting_can_help(self, tick: int, pos: int) -> bool:
        """空等有意义当且仅当：片单剩余项中至少一帧此刻尚不能确认完整，
        而在下一整数拍查询时变为完整（Fraction 时刻与整数拍解耦，1/2 刻
        到达的证据在整数拍 1 即被看到）。没有这种帧时等待纯属浪费一拍。
        """

        now, next_tick = Fraction(tick), Fraction(tick + 1)
        for frame_no in self.playlist[pos:]:
            current = self.album.view_at(frame_no, now).status
            upcoming = self.album.view_at(frame_no, next_tick).status
            if (
                current is not FrameStatus.COMPLETE
                and upcoming is FrameStatus.COMPLETE
            ):
                return True
        return False

    def _loadable_full_frames(
        self, at: Fraction, pos: int, base: object
    ) -> list[int]:
        wanted_bases: set[int] = set()
        for frame_no in self.playlist[pos:]:
            view = self.album.view_at(frame_no, at)
            if view.kind == sf.KIND_DELTA and view.base_no is not None:
                wanted_bases.add(view.base_no)
        result = []
        for full_no in sorted(wanted_bases):
            if full_no == base:
                continue
            view = self.album.view_at(full_no, at)
            if view.status is FrameStatus.COMPLETE and view.kind == sf.KIND_FULL:
                result.append(full_no)
        return result

    # ------------------------------------------------------------------
    def _physical_blocker(self) -> tuple[int, str]:
        """无可行枝时，定位片单上第一个永远无法完成的帧及原因。"""

        for frame_no in self.playlist:
            final = self.album.view_at(frame_no, Fraction(self.curtain))
            if final.status is FrameStatus.CONFLICT:
                return frame_no, "evidence conflict never resolved"
            if final.status is not FrameStatus.COMPLETE:
                return frame_no, f"still {final.status.value} at curtain"
            rendered = self.album.chain_check(frame_no, Fraction(self.curtain))
            if not rendered.ok:
                return rendered.blocker, rendered.reason
        return self.playlist[-1], "not enough beats for required waits/loads"

"""实况册 CLI 子进程端到端：登记 → 盘面 → 照片/线 → 发行 → 签发/重查/同次判定。

运行：python -m tests.demo_livebook_cli
"""

import json
import shutil
import subprocess
import sys
import tempfile

from . import livebook_scenarios as sc


def run_cli(data_root, *args, stdin_payload=None):
    cmd = [sys.executable, "-m", "budget_solver.livebook.cli",
           "--data-root", data_root, *args]
    payload = json.dumps(stdin_payload, ensure_ascii=False) if stdin_payload is not None else None
    proc = subprocess.run(cmd, input=payload, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"命令失败 {args}\nSTDERR={proc.stderr}"
    return json.loads(proc.stdout)


def put_model_cli(data_root, payload):
    cmd = [sys.executable, "-m", "budget_solver.cli",
           "--data-root", data_root, "put-model", "-"]
    proc = subprocess.run(cmd, input=json.dumps(payload), capture_output=True,
                          text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def main():
    data_root = tempfile.mkdtemp(prefix="lb-cli-demo-")
    try:
        put_model_cli(data_root, sc.bridge_model())
        snap = subprocess.run(
            [sys.executable, "-m", "budget_solver.cli", "--data-root", data_root,
             "snapshot", sc.MODEL_ID], capture_output=True, text=True, check=False)
        snap_id = json.loads(snap.stdout)["snap_id"]
        comp = subprocess.run(
            [sys.executable, "-m", "budget_solver.cli", "--data-root", data_root,
             "compile", snap_id], capture_output=True, text=True, check=False)
        artifact_hash = json.loads(comp.stdout)["artifact_hash"]
        book = sc.livebook_payload(artifact_hash)
        run_cli(data_root, "lb-register", "-", stdin_payload=book)
        print("1. 实况册登记：", run_cli(data_root, "lb-list"))
        print("2. 可达盘面：",
              [(p["plate_id"], p["moved_groups"]) for p in run_cli(data_root, "lb-plates", "BOOK_AB")])
        print("3. 盘面 P01 独查：",
              run_cli(data_root, "lb-inspect", "BOOK_AB", "P01")["needles"])

        run_cli(data_root, "lb-lines", "BOOK_AB", "-",
                stdin_payload={"lines": sc.lines()})
        photos = {"photoset_id": "F1", "photos": [sc.photo_both_needles()]}
        ps = run_cli(data_root, "lb-photos", "BOOK_AB", "-", stdin_payload=photos)
        issuance = run_cli(data_root, "lb-issue", ps["photoset_uid"])
        uid = issuance["issuance_uid"]
        print("4. 甲挡发行区段：", issuance["lines"]["JIA"]["explainable_gains"])

        cert = run_cli(data_root, "lb-certify", uid, "JIA", "7/4", "2")
        assert cert["certified"]
        print("5. 甲铭牌 [7/4,2] 整段签发：", cert["certified"])

        explain = run_cli(data_root, "lb-explain", uid, "JIA", "7/4")
        assert explain["shots"][0]["plate_id"] == "P01"
        print("6. g=7/4 见证：", [(s["photo_id"], s["plate_id"]) for s in explain["shots"]])

        ps2 = run_cli(data_root, "lb-photos", "BOOK_AB", "-",
                      stdin_payload={"photoset_id": "F2", "photos": [sc.photo_x_point()]})
        is2 = run_cli(data_root, "lb-issue", ps2["photoset_uid"])
        recheck = run_cli(data_root, "lb-recheck", is2["issuance_uid"], "YI",
                          '["2","4"]', "1")
        print("7. 驻点复检（夹一单）：最少单", recheck["min_sheets_needed"],
              "现场重查", recheck["on_site_lookups"])

        mixed = run_cli(data_root, "lb-photos", "BOOK_AB", "-", stdin_payload={
            "photoset_id": "MIXED",
            "photos": [sc.photo_both_needles(order=0), sc.photo_x_point(order=1)],
        })
        common = run_cli(data_root, "lb-common", mixed["photoset_uid"], "JIA")
        assert not common["feasible"]
        print("8. 两照片同次单向切换判定：", common["feasible"], "（", common["reason"], "）")

        print("\n实况册 CLI 端到端演示全部通过。数据目录：", data_root)
    finally:
        shutil.rmtree(data_root)


if __name__ == "__main__":
    main()

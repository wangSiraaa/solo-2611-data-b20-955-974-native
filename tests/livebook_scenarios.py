"""题面合成台架：电桥冻结模型、两条挡位线与三套实抄照片。

冻结公式（A=B=0 起值）::

    X = 1 + A，Y = 1 + B

* 甲挡 JIA：b = 1/2（u=0, v=1/2），正增益区间 g ∈ [1, 4]；
* 乙挡 YI ：b = 0（u=0, v=0），正增益区间 g ∈ [1, 4]。

全部数值为手工可核对的精确分数，断言见 :mod:`tests.test_livebook`。
"""

MODEL_ID = "BRIDGE"
X_VAR = "BRIDGE::X"
Y_VAR = "BRIDGE::Y"


def bridge_model():
    return {
        "model_id": MODEL_ID,
        "name": "电桥冻结模型",
        "cells": {
            "A": {"kind": "input", "value": "0"},
            "B": {"kind": "input", "value": "0"},
            "X": {"kind": "formula", "constant": "1",
                  "terms": [{"coef": "1", "cell_id": "A"}]},
            "Y": {"kind": "formula", "constant": "1",
                  "terms": [{"coef": "1", "cell_id": "B"}]},
        },
        "outputs": ["X", "Y"],
    }


def lines():
    return [
        {"line_id": "JIA", "u": "0", "v": "1/2", "g_min": "1", "g_max": "4"},
        {"line_id": "YI", "u": "0", "v": "0", "g_min": "1", "g_max": "4"},
    ]


def livebook_payload(artifact_hash, *, order_ab=True, book_id="BOOK_AB"):
    """A 先 B（order_ab=True）或 B 先 A（False）的实况册登记负载。"""
    ga_req = [] if order_ab else ["GB"]
    gb_req = ["GA"] if order_ab else []
    return {
        "book_id": book_id,
        "artifact": artifact_hash,
        "groups": [
            {"group_id": "GA", "requires": ga_req,
             "cells": [{"cell_id": "A", "start": "0", "to": "1"}]},
            {"group_id": "GB", "requires": gb_req,
             "cells": [{"cell_id": "B", "start": "0", "to": "1"}]},
        ],
        "needles": [
            {"cell_id": "X", "label": "X"},
            {"cell_id": "Y", "label": "Y"},
        ],
    }


# 照片一：X∈[4,5]、Y∈[2,3]（甲挡实抄）
def photo_both_needles(source_line="JIA", order=0):
    return {
        "photo_id": "P_BOTH", "order": order, "source_line": source_line,
        "readings": [
            {"variable": X_VAR, "lo": "4", "hi": "5"},
            {"variable": Y_VAR, "lo": "2", "hi": "3"},
        ],
    }


# 照片二：只见 X=4（精确点读数，乙挡实抄）
def photo_x_point(source_line="YI", order=0):
    return {
        "photo_id": "P_XPOINT", "order": order, "source_line": source_line,
        "readings": [{"variable": X_VAR, "lo": "4", "hi": "4"}],
    }


# 照片三：只见 X∈[2,4]（宽区间，连拍遮挡那类）
def photo_x_wide(order=0):
    return {
        "photo_id": "P_XWIDE", "order": order,
        "readings": [{"variable": X_VAR, "lo": "2", "hi": "4"}],
    }

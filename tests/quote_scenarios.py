"""数量报价曲线与采购额度核对的合成模型工厂。

全部数值手工可核对，与 :mod:`tests.scenarios` 同一风格。
"""


def cyclic_rate_quote_model(model_id="M_QUOTE", q_coef="1/4", q_frozen="0"):
    """循环费率报价模型（题目主例）：

        x = 2 + (q_coef)·q + (1/2)·x      => x = 4 + 2·q_coef·q

    冻结时把订量输入格 q 的点值设为 0（原点单价恒为 4），报价员再令 q 取
    报价域整数，由实际方程推出单价曲线，而不是把 q=0 的一次解向前沿用。
    """
    return {
        "model_id": model_id,
        "name": "循环费率数量报价模型",
        "cells": {
            "q": {"kind": "input", "value": q_frozen},
            "x": {
                "kind": "formula",
                "constant": "2",
                "terms": [
                    {"coef": q_coef, "cell_id": "q"},
                    {"coef": "1/2", "cell_id": "x"},
                ],
            },
        },
        "outputs": ["x"],
    }


def partial_consistency_cycle(model_id="M_PART"):
    """只有某个订量的方程才相容的真实循环例。

    循环块::

        a = b + 3            => a - b = 3
        b = a - 2q + 1       => -a + b + 2q = 1

    两式相加得 2q = 4，即只有 q=2 相容；其余订量出现 0 = 非零常数。
    单价格 c = a - b + 6 消去自由格，在 q=2 时唯一为 9（非负可报价）。
    """
    return {
        "model_id": model_id,
        "name": "部分订量相容循环",
        "cells": {
            "q": {"kind": "input", "value": "0"},
            "a": {
                "kind": "formula",
                "constant": "3",
                "terms": [{"coef": "1", "cell_id": "b"}],
            },
            "b": {
                "kind": "formula",
                "constant": "1",
                "terms": [
                    {"coef": "1", "cell_id": "a"},
                    {"coef": "-2", "cell_id": "q"},
                ],
            },
            "c": {
                "kind": "formula",
                "constant": "6",
                "terms": [
                    {"coef": "1", "cell_id": "a"},
                    {"coef": "-1", "cell_id": "b"},
                ],
            },
        },
        "outputs": ["c"],
    }


def multi_price_cycle(model_id="M_MULTI"):
    """同一订量有多个单价：a=b 自由循环，单价取 a，自由格无法消去。"""
    return {
        "model_id": model_id,
        "name": "同订量多单价循环",
        "cells": {
            "q": {"kind": "input", "value": "0"},
            "a": {
                "kind": "formula",
                "constant": "0",
                "terms": [{"coef": "1", "cell_id": "b"}],
            },
            "b": {
                "kind": "formula",
                "constant": "0",
                "terms": [{"coef": "1", "cell_id": "a"}],
            },
        },
        "outputs": ["a"],
    }


def fully_contradictory_cycle(model_id="M_CT"):
    """完全矛盾：a=b+1；b=a+1，相加得 0=2，与订量无关。"""
    return {
        "model_id": model_id,
        "name": "完全矛盾循环",
        "cells": {
            "q": {"kind": "input", "value": "0"},
            "a": {
                "kind": "formula",
                "constant": "1",
                "terms": [{"coef": "1", "cell_id": "b"}],
            },
            "b": {
                "kind": "formula",
                "constant": "1",
                "terms": [{"coef": "1", "cell_id": "a"}],
            },
        },
        "outputs": ["a"],
    }


def negative_price_model(model_id="M_NEG"):
    """单价 p = 2 - q：q=1,2 非负可报价，q≥3 为负价不可报价区段。"""
    return {
        "model_id": model_id,
        "name": "负价区段模型",
        "cells": {
            "q": {"kind": "input", "value": "0"},
            "p": {
                "kind": "formula",
                "constant": "2",
                "terms": [{"coef": "-1", "cell_id": "q"}],
            },
        },
        "outputs": ["p"],
    }


def main_rebate_spec():
    """题目主例价格表：1–3 件不返，4–8 件返 15；预算 12。"""
    return {
        "domain": [1, 8],
        "budget": "12",
        "tiers": [
            {"lo": 1, "hi": 3, "rebate": "0"},
            {"lo": 4, "hi": 8, "rebate": "15"},
        ],
    }


def rebate_start_at_three_spec():
    """主例变体：返 15 的起点改为 3（1–2 不返，3–8 返 15），预算仍 12。"""
    return {
        "domain": [1, 8],
        "budget": "12",
        "tiers": [
            {"lo": 1, "hi": 2, "rebate": "0"},
            {"lo": 3, "hi": 8, "rebate": "15"},
        ],
    }

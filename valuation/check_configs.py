# -*- coding: utf-8 -*-
"""把一批假设留档 config 过一遍 v2 校验层——改阈值后的回归工具。

用法: python valuation/check_configs.py CONFIG_DIR [FACTS_DIR]
  CONFIG_DIR  含 *_config.json（或 config.json）的目录，如 review 包的 sample-reports/
  FACTS_DIR   可选。含 {TICKER}/facts.json 或 {TICKER}_facts.json 时，对应标的做
              **完整校验**（含反双重计数与 margins 谷底——这两条需要 rev0/FCF 利润率）；
              缺 facts 的标的只查参数边界与跨情景排序。

判定基准（2026-07-22 落地时实测）：review 的 8 份样例 config 应当全部 PASS。
放宽阈值后若仍全 PASS 说明没放过头；收紧后若有样例被拦，先确认是不是误伤。
退出码：全过 0，有拦截 1。
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.valuation_service import _validate_judgment  # noqa: E402


def _facts_for(ticker, facts_dir):
    """facts.json 的两种常见摆法：{FACTS_DIR}/{T}/facts.json 与 {FACTS_DIR}/{T}_facts.json。
    再兜一层 jobs/ 风格的 {T}_*/facts.json（服务跑完留在 jobs/ 里的就是这个形状）。"""
    if not facts_dir:
        return None
    cands = [os.path.join(facts_dir, ticker, "facts.json"),
             os.path.join(facts_dir, f"{ticker}_facts.json")]
    cands += sorted(glob.glob(os.path.join(facts_dir, f"{ticker}_*", "facts.json")))
    for p in cands:
        if os.path.exists(p):
            return p
    return None


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cfg_dir, facts_dir = argv[1], (argv[2] if len(argv) > 2 else None)
    paths = sorted(glob.glob(os.path.join(cfg_dir, "*config*.json")))
    if not paths:
        print(f"{cfg_dir} 下没找到 *config*.json")
        return 2

    print(f"{'标的':8s} {'模式':11s} {'深度':10s} 结果")
    print("-" * 96)
    fails = 0
    for p in paths:
        cfg = json.load(open(p, encoding="utf-8"))
        ticker = cfg.get("ticker") or os.path.basename(p).split("_")[0]
        mode = cfg.get("mode") or "standard"
        rev0 = fcfm = band = None
        depth = "边界+排序"
        fp = _facts_for(ticker, facts_dir)
        if fp and mode == "standard":
            fj = json.load(open(fp, encoding="utf-8"))
            # band 必须与产线同源传入：0050 起低倍数票 pe 下限带 band 自适应，
            # 不传会用静态 8 误拦产线能过的 config（回归工具比产线严=假警报）
            band = fj.get("pe_band")
            ttm = fj["ttm"]
            try:
                rev0 = ttm["revenue"]["value"] / 1e6
                fcfm = ((ttm["cfo"]["value"] - ttm["capex"]["value"])
                        / ttm["revenue"]["value"])
                depth = "完整"
            except (KeyError, TypeError, ZeroDivisionError):
                rev0 = fcfm = None   # facts 不全就退回边界检查，不让工具自己崩
        try:
            _validate_judgment(cfg, mode, rev0=rev0, fcf_margin=fcfm, band=band)
            print(f"{ticker:8s} {mode:11s} {depth:10s} PASS")
        except Exception as e:                      # noqa: BLE001 — 校验层抛什么都算拦截
            fails += 1
            print(f"{ticker:8s} {mode:11s} {depth:10s} BLOCK  {str(e)[:60]}")

    print(f"\n{len(paths)} 份：{len(paths) - fails} PASS / {fails} BLOCK")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

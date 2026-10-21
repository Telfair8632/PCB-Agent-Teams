"""check_chirality 行为锁定测试 — 全部合成 fixture, 不依赖任何具体项目。

关键性质:
  1. 正确的双排蛇形 IC → serpentine_ok; 它的 y 镜像 → cw_serpentine
  2. pitch >= 4mm 的双排(变压器类)永不进 cw_serpentine
  3. 名字带 Reverse / Clockwise → suppressed_variant
  4. B.Cu 封装绕向要求相反
  5. 群体判据: >= 2 且多于 serpentine_ok 才 hard_fail
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "tools"))
import check_chirality as cc  # noqa: E402


def soic8(mirror=False, pitch=1.27, half_gap=2.475):
    """官方 SOIC-8 风格引脚坐标: 1..4 沿左列下行, 5..8 沿右列上行(逆时针)。"""
    ys = [(-1.5 * pitch) + i * pitch for i in range(4)]
    pins = {}
    for i in range(4):
        pins[i + 1] = (-half_gap, ys[i])
        pins[8 - i] = (half_gap, ys[i])
    if mirror:
        pins = {n: (x, -y) for n, (x, y) in pins.items()}
    return pins


def test_correct_soic_is_ok():
    assert cc.judge(soic8(), True)[0] == "serpentine_ok"


def test_mirrored_soic_flagged():
    assert cc.judge(soic8(mirror=True), True)[0] == "cw_serpentine"


def test_back_side_inverts_requirement():
    # 翻到背面的封装在文件里本来就是镜像形态 → 镜像坐标 + B.Cu = 合法
    assert cc.judge(soic8(mirror=True), False)[0] == "serpentine_ok"
    assert cc.judge(soic8(), False)[0] == "cw_serpentine"


def test_transformer_pitch_excluded():
    # 双排蛇形但 pitch 5mm(变压器类): 不进 cw_serpentine, 降为 suspect
    v = cc.judge(soic8(mirror=True, pitch=5.0, half_gap=10.0), True)[0]
    assert v == "cw_suspect"


def test_variant_name_suppressed():
    v = cc.judge(soic8(mirror=True), True, name="TSOP-I-32_Reverse")[0]
    assert v == "suppressed_variant"
    v = cc.judge(soic8(mirror=True), True, name="LGA-8_ClockwisePinNumbering")[0]
    assert v == "suppressed_variant"


def test_triangle_connector_not_hard():
    # 3 脚三角(XLR 类): 顺时针也只是 suspect — 厂家编号无逆时针约定
    pins = {1: (3.0, 2.0), 2: (-3.0, 2.0), 3: (0.0, -2.0)}
    assert cc.judge(pins, True)[0] == "cw_suspect"


def test_collinear_header_not_judged():
    pins = {1: (0.0, 0.0), 2: (2.54, 0.0), 3: (5.08, 0.0)}
    assert cc.judge(pins, True)[0] == "not_judgeable"


def test_esop_ep_retry():
    pins = soic8(mirror=True)
    pins[9] = (0.0, 0.0)          # 中央 EP
    assert cc.judge(pins, True)[0] == "cw_serpentine"


def _mod(name, pins):
    pads = "\n".join(
        f'  (pad "{n}" smd rect (at {x} {y}) (size 0.6 1.5) (layers F.Cu))'
        for n, (x, y) in sorted(pins.items()))
    return f'(footprint "{name}"\n  (layer "F.Cu")\n{pads}\n)\n'


def _run_cli(tmp_path, mods):
    lib = tmp_path / "t.pretty"
    lib.mkdir()
    for name, pins in mods:
        (lib / f"{name}.kicad_mod").write_text(_mod(name, pins), encoding="utf-8")
    p = subprocess.run(
        [sys.executable, str(Path(cc.__file__)), str(lib)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, json.loads(p.stdout)


def test_population_rule_two_mirrored_fails(tmp_path):
    code, out = _run_cli(tmp_path, [("A", soic8(mirror=True)),
                                    ("B", soic8(mirror=True))])
    assert code == 2 and out["hard_fail"] and out["verdict"] == "MIRRORED_LIBRARY"


def test_population_rule_single_hit_lists_not_fails(tmp_path):
    # 单件顺时针: 列名单要求处置, 但不自动挡(可能是厂家编号)
    code, out = _run_cli(tmp_path, [("A", soic8(mirror=True)),
                                    ("B", soic8()), ("C", soic8())])
    assert code == 0 and not out["hard_fail"]
    assert out["verdict"] == "VERIFY_LISTED" and len(out["cw_serpentine"]) == 1


def test_clean_library(tmp_path):
    code, out = _run_cli(tmp_path, [("A", soic8()), ("B", soic8())])
    assert code == 0 and out["verdict"] == "CLEAN"

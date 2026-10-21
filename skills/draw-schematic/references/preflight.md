# Preflight — 环境检查 + 工具栈

`pipeline.py` 内部已实现这个检查，fail-fast。手工跑 / 排查路径问题时参考。

## KiCad CLI 路径自动定位

```bash
KICAD_CLI=""
for p in \
  "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli" \
  "/usr/bin/kicad-cli" \
  "/usr/local/bin/kicad-cli" \
  "/snap/kicad/current/bin/kicad-cli" \
  "C:/Program Files/KiCad/10.0/bin/kicad-cli.exe"
do
  [ -f "$p" ] && KICAD_CLI="$p" && break
done
[ -z "$KICAD_CLI" ] && KICAD_CLI=$(which kicad-cli)
[ -z "$KICAD_CLI" ] && echo "❌ kicad-cli 找不到，装 KiCad 10" && exit 1
echo "✓ kicad-cli: $KICAD_CLI"
```

## Python venv

```bash
VENV=".venv/bin/python"
[ ! -f "$VENV" ] && echo "❌ venv 不存在" && exit 1
echo "✓ venv: $VENV"
```

激活方式（不需要 source，直接调）：
```bash
".venv/bin/python" <脚本>
```

## 工具栈（已装在 `PCB-Agent-Teams/.venv/`）

| 工具 | 干嘛 | 版本 |
|---|---|---|
| **circuit-synth** | Python DSL → `.kicad_sch` + `.kicad_pro` + 网表 | 0.12.1 |
| **kicad-sch-api** | 读取 KiCad sch 文件 + 精确 pin 坐标查询 | 0.5.6 |
| **easyeda2kicad** | component-selecting 的底层库转换工具，draw-schematic 不直接调用 | 1.0.1 |
| **kicad-cli** | KiCad 命令行（ERC、出 PDF）| 10.0.1 |
| **check-schematic analyzer** | 结构化 schematic JSON，供 SPICE 子电路仿真 / release umbrella 文档消费 | 本工作区 |
| **check-schematic SPICE** | 子电路 SPICE 仿真（regulator / divider / RC / LC / opamp / crystal）| 本工作区 |

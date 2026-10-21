# 工作时的输出格式

完成生成 → 给用户两个文件路径：

```
✅ 完成
  • <Project>.pdf       ← Finder 双击看图
  • <Project>.kicad_pro ← KiCad 双击编辑

L1 ERC: total_errors=0（按 type 分类显示），footprint warning N 个（非电气）
L2 视觉: 看过 PDF，元件 + wire 连接清晰
L3 拓扑: 跟项目 CLAUDE.md 完整原理图段 100% 一致

下一步建议：
  • check-schematic → 跑深度 sch review + SPICE 仿真（Phase 5 检查 gate）
  • 通过后 → draw-pcb 进 PCB 布局
  • OR 改 BOM 后重跑此 pipeline
```

## 禁止报告

- ❌ "完成" 但只看 ERC 数字（必须 L2 看 PDF）
- ❌ 工具回 `success: true` 直接相信（mixelpixx 假阳性教训）
- ❌ 不看 PDF 视觉就说 "可读性好"
- ❌ **只 grep 一种 ERC 错误**（教训：4 月 27 日只数 `pin_not_connected = 0` 就报通过，实际还有 18 个其他类型的真错被放过）。永远用 `total_errors == 0` 作门槛。

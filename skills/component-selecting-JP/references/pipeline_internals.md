# Phased Pipeline（脚本内部）

```
Phase 0  SIZE CHECK    → longlist > 25 直接 fail；> 10 警告但继续
Phase 1  LIBRARY PROBE → 离线 lib_external + lib_cache 扫描（毫秒级）
                          ↓ library 状态独立报告，**不阻止** vendor API 调用
                          ↓ （早期选品 user 经常需要"library 还没拉但价格如何"）
                          ↓ 例外：role ∈ {capacitor, resistor, ferrite_bead,
                          ↓ inductor_smd, inductor_th} 时整段跳过，
                          ↓ library_gate 自动 pass（KiCad std footprint 库
                          ↓ 已覆盖所有 0402/0603/0805/1206 等通用尺寸）
Phase 2  VENDOR API   → DK_JP + Mouser_JP + LCSC 并行（worker=2，跨进程 throttle）
                          所有候选都打，无关 library 状态
Phase 3  VERDICT       → buyable_gate + library_gate + solderability_gate
                          + 价格 tag + 排名（library 缺 → verdict=fail，
                          但 vendor_results 数据完整供 user 查看）
```

> **历史教训（2026-05-07 修复）**：之前 Phase 1 library_probe fail 的候选会直接跳过 vendor API（"省 daily quota"），导致 user 在选品早期看不到价格信号。实测该优化挡住了正常的"调研"用例，已删除。current quota 75 calls/longlist（25 × 3 lanes）远低于 1000/day，省的那点配额不值得阻塞 user 视野。

**为什么 worker=2**：DK throttle 600ms / Mouser 2.2s 是单源瓶颈，>2 worker 在锁上排队没收益（特别是 3 个 sub-agent 并行时）。

## 退出码

- `0`：至少一个候选 pass 或 warn_single_source
- `2`：所有候选 fail
- `3`：缺输入 / longlist 超硬上限 25 / locale unknown

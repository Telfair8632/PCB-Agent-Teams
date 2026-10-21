# 反模式

- ❌ **没问"哪些已定"就开干**：用户已锁定拓扑/锚点件，本 skill 还跑完整流程
- ❌ **重新展开 legacy 已有的详细内容**：参数验算 / 安规 checklist / 哲学
- ❌ **CLAUDE.md 写过详**：堆 200 行参数推演让未来 AI 看懵；保持简洁让 AI 懂结构即可
- ❌ **讨论期间跑严格 longlist + library 三件套**：这是 component-selecting Phase 2.0 该做的事，本 skill 跑就是越权
- ❌ **sub-agent prompt 过度膨胀**：辅助查询给一两行 spec + role 即可，不要塞 200 行背景
- ❌ **跳 Step 0 扫盲**：相关 [待填] 项不问，到锚点选完才发现用户测不了
- ❌ **硬编码 locale / vendor 名**
- ❌ **抄 reference design**：照搬 typical application，没核对 V_DD 双侧和地策略
- ❌ **只给一个候选**：让用户失去对比锚点
- ❌ **没说"下一步去 component-selecting"就退出**：用户不知道严格审查环节在哪

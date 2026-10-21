# 脚本入口 + 内部 phase

## 命令行

### longlist 验证（标准路径）

```bash
python3 skills/component-selecting-JP/scripts/component_select.py \
  --longlist <path>/<role>_longlist.json \
  --project-path <project_or_tmp_path> \
  --expected-role <role> \
  --fetch-web --summary \
  --output <path>/<role>_shortlist.json
```

### Single MPN（更快路径，跳过 longlist）

```bash
python3 skills/component-selecting-JP/scripts/component_select.py \
  --mpn "<MPN>" --project-path <path> \
  --expected-role <role> --fetch-web --summary
```

## 路径约定

- `--longlist` 输入：项目内 `Projects/<name>/datasheets/component_selecting/<role>_longlist.json`，
  或独立调研 `/tmp/<sandbox>/datasheets/component_selecting/<role>_longlist.json`
- `--output` 输出：项目内 `Projects/<name>/_artifacts/component_selecting/<role>_shortlist.json`，
  或独立调研 `/tmp/<sandbox>/_artifacts/component_selecting/<role>_shortlist.json`
- `--project-path` 用于 lib_external/cache 探测和路径作用域；独立调研用 `/tmp/<sandbox>` 即可

## 特定 role flag：TVS

TVS 必传 `--expected-role tvs` 让脚本抽 V_RWM / V_BR 参数（ROLE_PROFILE 已配 alias）。
其余关键件（IC / 隔离器 / DC-DC / 控制器 / ADC / MOSFET / HV 连接器）传对应 snake_case role 名即可。

## 脚本内部 phase

`SIZE CHECK → LIBRARY PROBE → VENDOR API (worker=2) → VERDICT`。

详细 phase 内部 + 历史教训 + 退出码 → `pipeline_internals.md`。
DK/Mouser daily quota 计算 → `quota_math.md`。

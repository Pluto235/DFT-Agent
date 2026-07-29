# 字段规范：`material`

> 材料字段的组织方式——特别是 `initial_structure` 给 agent 的几何颗粒度（完整坐标 vs formula+space_group，见 open_questions 第 3 条）——是 bench 设计的重要选择。请专家看看这种组织是否合适、是否暴露了不该暴露的线索。

## 用途

描述材料本身（化学式、结构、对称性）与 DFT 计算所需的赝势信息。字段内容的一部分用来拼成 agent 的 prompt（见下），另一部分用于评分侧的溯源与 evaluator。

## 关于 agent 如何看到本字段

**agent 不会直接接收原始 JSON**。Bench harness 会从 material 字段中选取"给 agent"列标 ✓ 的子字段，拼成一段自然语言 + QE input 片段混合的文本提供给 agent。下面表格里标 ✗ 的子字段（如 `note`、`reference_source`、`initial_structure.source` 等）只用于 bench 内部溯源与 reviewer 阅读，不进入 prompt。

典型 agent 可见的拼接结果见 `agent_visible.md`。完整拼接规则以各子字段表格中的"给 agent"列为准。

## JSON 片段

```json
"material": {
  "formula": "Si",
  "name": "silicon (diamond)",
  "structure": "diamond",
  "space_group": "Fd-3m",
  "space_group_number": 227,
  "atoms_per_primitive_cell": 2,
  "advisory_notes": null,
  "reference_source": {
    "database": "Materials Project",
    "id": "mp-149",
    "url": "https://next-gen.materialsproject.org/materials/mp-149"
  },
  "initial_structure": {
    "source": "mp-149",
    "note": "Pre-relaxed from MP; agent should relax",
    "lattice_type": "primitive",
    "lattice_constants_angstrom": {"a": 3.866, "b": 3.866, "c": 3.866},
    "lattice_angles_degree": {"alpha": 60.0, "beta": 60.0, "gamma": 60.0},
    "atomic_positions_crystal": [
      {"element": "Si", "position": [0.0, 0.0, 0.0]},
      {"element": "Si", "position": [0.25, 0.25, 0.25]}
    ]
  },
  "pseudopotential": {
    "family": "GBRV_USPP_PBE_v1.5",
    "relativistic": "scalar",
    "files": {"Si": "Si.upf"}
  }
}
```

## 子字段规范

| 子字段 | 必填 | 类型 | 给 agent | 说明 |
|---|---|---|---|---|
| formula | 是 | string | ✓ | 化学式，如 "Si"、"CsPbBr3" |
| name | 否 | string | ✗ | 可读名称，如 "silicon (diamond)"；仅供 bench 内部 reviewer 使用，不进 agent prompt |
| structure | 是 | string | ✓ | 结构原型，如 "diamond"、"zinc blende"、"cubic perovskite"；**禁止括号表达**（形如 "perovskite (cubic)" 会导致 prompt 里二层括号） |
| space_group | 是 | string | ✓ | 国际符号，如 "Fd-3m" |
| space_group_number | 是 | integer | ✗ | 空间群编号（整数），参与评分的 exact 字段；不给 agent |
| atoms_per_primitive_cell | 是 | integer | ✓ | 原胞内原子数 |
| advisory_notes | 是 | string / null | 条件 | 材料物理属性的特例提醒（如 AFM 磁矩、DFT+U、SOC）；非空时 harness 拼到 prompt 末尾，null 则不拼；详见下方约定 |
| reference_source | 否 | object | ✗ | 数据库来源；**严禁给 agent**（泄题） |
| initial_structure | 是 | object | 部分 | 见下表，只有几何信息给 agent |
| pseudopotential | 是 | object | 部分 | 见下表 |

### `reference_source`（整体不给 agent）

| 子字段 | 必填 | 类型 | 给 agent | 说明 |
|---|---|---|---|---|
| database | 是 | string | ✗ | "Materials Project" / "ICSD" / "用户自制" 等 |
| id | 是 | string | ✗ | 该数据库内的材料 ID，如 "mp-149" |
| url | 否 | string | ✗ | 网页链接 |

给 agent 看到 `id=mp-149` 相当于让他直接调 Materials Project API 拿到 a=5.43 Å 的参考值——绕开 DFT 计算直接作弊。**整个 reference_source 严格限制在 bench 内部**。

### `initial_structure`

Agent 可见的几何信息部分，格式贴近 QE input 便于 harness 直接拼接到 prompt。

| 子字段 | 必填 | 类型 | 给 agent | 说明 |
|---|---|---|---|---|
| source | 否 | string | ✗ | 引用 reference_source.id；与 reference_source 同样理由不给 |
| note | 否 | string | ✗ | bench 作者内部备注（如"已弛豫，agent 无需再弛豫"），只给 reviewer 看 |
| lattice_type | 是 | string | ✓ | `primitive` / `conventional` |
| lattice_constants_angstrom | 是 | object | ✓ | a/b/c（angstrom） |
| lattice_angles_degree | 是 | object | ✓ | α/β/γ（degree） |
| atomic_positions_crystal | 是 | array | ✓ | 原子坐标列表（分数坐标） |

### `pseudopotential`

| 子字段 | 必填 | 类型 | 给 agent | 说明 |
|---|---|---|---|---|
| family | 是 | string | ✓ | 赝势族完整 ID，含版本（当前默认 `GBRV_USPP_PBE_v1.5`） |
| relativistic | 是 | string | ✓ | `scalar`（SR）/ `full`（FR，用于 SOC） |
| files | 是 | object | ✓ | 元素符号 → 赝势文件名映射 |
| note | 否 | string | ✗ | bench 内部备注 |

## 说明

- 赝势文件通过 `files` 引用相对路径，实际文件存在 `pseudos/<family>/` 下
- `lattice_type` 选择 primitive 还是 conventional 影响晶格常数数值（primitive 更小，conventional 更符合教材），evaluator 侧需与此保持一致
- `pseudopotential.relativistic` 默认 `scalar`；M5 SOC 材料必须用 `full`
- v1.2 起默认赝势族改为 **GBRV USPP PBE v1.5**（杨博 0423 意见），取代原先的 PseudoDojo NC 赝势。GBRV 为 ultrasoft + scalar relativistic，只提供 SR 版本；SOC 题目（M5）需单独指定其他族（如 ONCVPSP full-relativistic）

## `advisory_notes` 使用约定

- **起步 16 题全部填 `null`**（即不启用），朴素 prompt 不带任何材料特例提醒
- 未来在 M3+ 材料（磁性、强关联、vdW/SOC）扩展时，可能按题启用——是否给 agent 提示材料特性（考察"认识材料" vs "能执行"的不同维度）由伟霖 + 杨博届时确认
- 启用时内容应简短（1–3 句），只陈述材料物理事实 + 对应的操作提示，不给参数数值。举例（仅示意，当前不启用）：
    - M3 NiO：`"This material is antiferromagnetic; remember to set initial magnetic moments in starting_magnetization."`
    - M4 CuO：`"This material has strongly correlated d electrons; DFT+U may be required for a reasonable gap."`
    - M5 Bi2Se3：`"This material has strong spin-orbit coupling; use full-relativistic pseudopotentials and lspinorb=.true."`
- 区别于 `initial_structure.note` 和 `pseudopotential.note`——后两者是 bench 内部溯源备注，**不给 agent**；`advisory_notes` 是显式给 agent 的提示

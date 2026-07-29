# Prompt 模板（`bench/prompts/`）

本目录存放每个 task 的 prompt 模板文件。bench harness 按下列规则从题目 JSON 读取字段 → 填入模板 → 产出 agent 实际看到的 prompt 字符串。

设计决策背景见 `../prompt_generation_strategy.md`。

## 模板清单

| 文件 | 对应 task.type | task_tier |
|---|---|---|
| `vc_relax.txt` | vc_relax | T1 |
| `bands_dos.txt` | bands_dos | T2 |
| （待 P1 扩展） | dfpt_born_dielectric | T3 |
| （待 P1 扩展） | eos | T4 |

模板文件纯文本，不带 frontmatter，`{slot_name}` 为占位符，harness 用 Python `.format()` 填入。

## Slot 字典

下表列出所有模板用到的 slot，以及它来自题目 JSON 的哪个字段。harness 按此表取值，不允许增改。

### 单字段 slot（直接取值）

| slot | 来源字段（JSON 路径） | 类型 | 示例值 |
|---|---|---|---|
| formula | material.formula | string | "Si" |
| structure_type | material.structure | string | "diamond" |
| space_group | material.space_group | string | "Fd-3m" |
| atoms_per_primitive_cell | material.atoms_per_primitive_cell | integer | 2 |

> 注：`material.name`（如 "silicon (diamond)"）**不进 prompt**，仅供 bench 内部 reviewer 阅读使用。模板用 `formula` + `structure_type` 两个裸字段组合出材料描述，避免 `name` 自带括号导致的嵌套。
>
> `material.structure` 字段值应为**不带括号**的原型名（如 `"diamond"`、`"zinc blende"`、`"cubic perovskite"`）——禁止形如 `"perovskite (cubic)"` 这样的嵌套表达，会在括号列里产生二层括号。

### 复合 slot（按下节规则渲染）

| slot | 来源字段 | 说明 |
|---|---|---|
| initial_structure_block | material.initial_structure | CELL_PARAMETERS + ATOMIC_POSITIONS 两段 QE 原生语法 |
| pseudopotential_block | material.pseudopotential | 赝势族声明 + 元素 → 文件映射 |
| advisory_notes_block | material.advisory_notes | 条件拼接，null 时为空串 |

## 复合 slot 的渲染规则

### `initial_structure_block`

从 `material.initial_structure` 渲染为两段 QE 原生语法：

```
CELL_PARAMETERS angstrom
<a1x> <a1y> <a1z>
<a2x> <a2y> <a2z>
<a3x> <a3y> <a3z>

ATOMIC_POSITIONS crystal
<element_1> <frac_x_1> <frac_y_1> <frac_z_1>
...
```

晶格矩阵 `{a1, a2, a3}` 由 `lattice_constants_angstrom`（a/b/c）和 `lattice_angles_degree`（α/β/γ）按 Lennard-Jones 标准约定导出：

$$
\vec{a}_1 = (a, 0, 0)
$$
$$
\vec{a}_2 = (b \cos\gamma,\; b \sin\gamma,\; 0)
$$
$$
\vec{a}_3 = \left(c \cos\beta,\; c \frac{\cos\alpha - \cos\beta \cos\gamma}{\sin\gamma},\; c \frac{\sqrt{1 - \cos^2\alpha - \cos^2\beta - \cos^2\gamma + 2\cos\alpha\cos\beta\cos\gamma}}{\sin\gamma} \right)
$$

原子坐标 `ATOMIC_POSITIONS crystal` 分块直接从 `atomic_positions_crystal` 列表渲染。单位标签固定 `angstrom`（晶格）和 `crystal`（原子，即分数坐标）。

### `pseudopotential_block`

从 `material.pseudopotential` 渲染：

```
Use {family_description} pseudopotentials ({relativistic_description}):
<element_1> -> <file_1>
<element_2> -> <file_2>
...
```

其中：

- `family_description`：将 `family` 字符串的下划线换空格、保留原大小写。如 `GBRV_USPP_PBE_v1.5` → `GBRV USPP PBE v1.5`
- `relativistic_description`：`scalar` → `scalar relativistic`；`full` → `full relativistic`

### `advisory_notes_block`

当 `material.advisory_notes` 为 null（起步 16 题默认）：渲染为**空串**（不产生任何文字，不留空行）。

当 `material.advisory_notes` 为字符串且非空：渲染为：

```

Note: <advisory_notes_string>
```

（前置一个空行，让末尾与 pseudopotential_block 之间自然分隔）

## 拼接示例：M1_T1_Si_vc_relax

取 `examples/example_M1_T1_Si_vc_relax.json` 填入 `vc_relax.txt`，agent 实际看到：

```
For Si (diamond, space group Fd-3m, 2 atoms per primitive cell), perform a variable-cell relaxation (vc-relax) calculation using the provided pseudopotentials. The initial structure is given below. Choose appropriate convergence parameters. Converge stress below 0.5 kbar and forces below 1e-3 Ry/a.u. Return the fully relaxed structure and verify the space group is preserved.

CELL_PARAMETERS angstrom
3.866000 0.000000 0.000000
1.933000 3.348054 0.000000
1.933000 1.116018 3.156576

ATOMIC_POSITIONS crystal
Si 0.000000 0.000000 0.000000
Si 0.250000 0.250000 0.250000

Use GBRV USPP PBE v1.5 pseudopotentials (scalar relativistic):
Si -> Si.upf
```

由于 `advisory_notes` 为 null，最后无 Note 段。

### 关于晶格矩阵数值的说明

同一个 Si 钻石 primitive cell，晶格矩阵可写成 L-J 标准形式（上例所示）或对称形式（`(0, a/2, a/2), (a/2, 0, a/2), (a/2, a/2, 0)`，其中 a=5.468 Å 为 conventional 立方边长）。两者描述同一个 Bravais 格子，只是基变换。

bench 严格采用 L-J 标准形式（数值由 a/b/c/α/β/γ 唯一确定），以保证 harness 从 JSON 到 CELL_PARAMETERS 的渲染过程无歧义。如果题目设计者希望给 agent 特定形式的矩阵（例如让 Si 用对称形式），应直接把这种形式通过合适的 `{a, b, c, α, β, γ}` 参数化表达出来。

## Harness 拼接流程（伪代码）

```python
def render_prompt(question_json: dict) -> str:
    task_type = question_json["task"]["type"]
    template_path = f"bench/prompts/{task_type}.txt"
    template = open(template_path).read()

    slots = {
        "formula": question_json["material"]["formula"],
        "structure_type": question_json["material"]["structure"],
        "space_group": question_json["material"]["space_group"],
        "atoms_per_primitive_cell": question_json["material"]["atoms_per_primitive_cell"],
        "initial_structure_block": render_structure_block(
            question_json["material"]["initial_structure"]
        ),
        "pseudopotential_block": render_pseudo_block(
            question_json["material"]["pseudopotential"]
        ),
        "advisory_notes_block": render_advisory_block(
            question_json["material"]["advisory_notes"]
        ),
    }
    return template.format(**slots).rstrip() + "\n"
```

## 测试断言（harness 正确性）

将来实现 harness 时应配下列单元测试：

- 对起步 16 题，`render_prompt` 输出**确定**（同输入永远同输出，无随机性）
- `render_prompt` 的输出**不包含**任何以下字段值：
    - `material.reference_source.*`（id / url / database）
    - `material.initial_structure.source`、`.note`
    - `material.pseudopotential.note`
    - `material.space_group_number`（是 exact 评分字段）
    - `task.type`
    - `evaluator.*` / `ground_truth.*` / `scoring.*` / `convergence_evidence.*` / `metadata.*`
    - `difficulty.*`
- 当 `material.advisory_notes` 为 null 时，输出**不包含** "Note:" 字符串
- 当 `material.advisory_notes` 非空时，输出**以** "Note: <advisory_notes>" 结尾

## 与 agent_visible.md 的对应

本 README 的 Slot 字典 + 渲染规则 = `fields/agent_visible.md` 规范的**实现层落地**。两者必须保持一致：

- 本 README 列出的 slot 来源字段，必须在 `fields/agent_visible.md` "给 agent" 表中有对应行
- `fields/agent_visible.md` "不给 agent" 表的字段，绝不能出现在本 README 的 slot 字典里

如未来扩展字段，两份文档须同时更新。

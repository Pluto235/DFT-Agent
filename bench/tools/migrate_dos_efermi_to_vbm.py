"""
迁移 T2 insulator GT 从 nscf EF 参考切到 VBM 参考。

杨博 2026-05-18 拍板：dos_e_fermi 字段在金属返回 nscf EF；在绝缘体返回 VBM。
对应：dos_reference.dat 也用 VBM 作为减法参考（而非旧的 nscf EF）。

本脚本：
  - 扫描 M1 insulator（gap_type != 'metal' 且 band_gap > 0.1）的 question JSON
  - 从 canonical_gt_t2/<id>.json 拿 extracted_raw.vbm_ev
  - 读 canonical_gt_t2/dos_data/<material>.dat（raw 绝对能量）
  - 写新 questions/M1_T2/gt_data/<id>/dos_reference.dat：col1 = E_abs - VBM, 裁 [-5, +5]
  - 更新 ground_truth.dos_e_fermi.value = VBM
  - 跳过 canonical FAILED 的题目（无 VBM 数据时保留原 EF）

幂等：可重跑（每次从 canonical 源重算）。
"""
import json, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CGT = REPO / 'tools/gt_selfrun/canonical_gt_t2'
QDIRS = [REPO / 'questions/M1_T2']

# 材料名 → canonical dos_data/*.dat 名（去掉 M*_T*_ 前缀和 _bands_dos 后缀）
def material_dat_name(qid: str) -> str:
    # qid 形如 M1_T2_LiCl_bands_dos → LiCl
    parts = qid.replace('_bands_dos', '').split('_', 2)
    if len(parts) >= 3:
        return parts[2]
    return qid

def trim_to_window(e_abs, dos, ref_ev, lo=-5.0, hi=5.0):
    """返回 (e_rel_trimmed, dos_trimmed)，col1 = e_abs - ref_ev, 范围 [lo, hi]."""
    out_e, out_d = [], []
    for ea, d in zip(e_abs, dos):
        er = ea - ref_ev
        if lo <= er <= hi:
            out_e.append(er)
            out_d.append(d)
    return out_e, out_d

def parse_raw_dos_dat(path: Path):
    """解析 raw dos.dat: 返回 (col1 list, col2 list)."""
    e, d = [], []
    for line in path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith('#'): continue
        parts = s.split()
        if len(parts) >= 2:
            try: e.append(float(parts[0])); d.append(float(parts[1]))
            except ValueError: continue
    return e, d

def write_dos_reference(out_path: Path, e_rel, dos):
    """写 dos_reference.dat 格式（同 generate.py:_write_dos_reference）."""
    lines = ['# E_eV(relative_to_VBM)  DOS_states_per_eV_per_fu']
    for er, d in zip(e_rel, dos):
        lines.append(f'{er:.6f}  {d:.6f}')
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')

# ---------- main ----------
n_migrated = 0
n_skipped_no_vbm = 0
n_skipped_metal = 0
n_skipped_no_dat = 0
errors = []

for qdir in QDIRS:
    for qfile in sorted(qdir.glob('*_bands_dos.json')):
        q = json.load(open(qfile))
        gt = q.get('ground_truth', {})
        gtype = gt.get('gap_type', {}).get('value')
        gap = gt.get('band_gap_ev', {}).get('value')
        if gtype == 'metal' or (gap is not None and gap < 0.1):
            n_skipped_metal += 1
            continue

        qid = qfile.stem
        cn = CGT / f'{qid}.json'
        if not cn.exists():
            errors.append((qid, 'canonical_gt JSON missing'))
            continue

        c = json.load(open(cn))
        vbm = c.get('extracted_raw', {}).get('vbm_ev')
        if vbm is None:
            n_skipped_no_vbm += 1
            print(f"  SKIP {qid}: no vbm_ev (canonical status={c.get('status')})")
            continue

        # 找 raw dos.dat（canonical_gt_t2/dos_data/<material>.dat）
        mat_name = material_dat_name(qid)
        raw_dat = CGT / 'dos_data' / f'{mat_name}.dat'
        if not raw_dat.exists():
            n_skipped_no_dat += 1
            errors.append((qid, f'raw dos.dat missing: {raw_dat}'))
            continue

        e_abs, dos = parse_raw_dos_dat(raw_dat)
        if not e_abs:
            errors.append((qid, 'raw dos.dat empty'))
            continue

        # Build new dos_reference: trim to [-5, +5] eV relative to VBM
        e_rel, dos_t = trim_to_window(e_abs, dos, vbm, lo=-5, hi=5)

        # Determine output path from question JSON's ref_file (resolve from REPO root)
        ref_rel = gt.get('dos_curve', {}).get('value', {}).get('ref_file')
        if not ref_rel:
            errors.append((qid, 'no ref_file in ground_truth.dos_curve.value'))
            continue
        # ref_file 格式 'bench/questions/M*_T2/gt_data/<id>/dos_reference.dat'
        # 去掉前缀 'bench/' 得到相对 REPO 的路径
        ref_path = REPO / ref_rel.replace('bench/', '', 1)

        # Write new dos_reference.dat
        write_dos_reference(ref_path, e_rel, dos_t)

        # Update question JSON: ground_truth.dos_e_fermi.value = VBM
        old_ef = q['ground_truth']['dos_e_fermi']['value']
        q['ground_truth']['dos_e_fermi']['value'] = round(float(vbm), 4)
        # 加 note 字段说明这是 VBM (杨博 2026-05-18 切换)
        q['ground_truth']['dos_e_fermi']['rule'] = 'absolute'
        # 写回
        qfile.write_text(json.dumps(q, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

        n_migrated += 1
        if n_migrated <= 5 or n_migrated % 10 == 0:
            print(f"  [{n_migrated}] {qid}: EF {old_ef:.4f} → VBM {vbm:.4f}, "
                  f"dos_ref n={len(e_rel)}")

print(f"\n=== migration done ===")
print(f"migrated:    {n_migrated}")
print(f"skipped metal:        {n_skipped_metal}")
print(f"skipped no VBM (canonical FAILED): {n_skipped_no_vbm}")
print(f"skipped no raw dos.dat: {n_skipped_no_dat}")
if errors:
    print(f"errors ({len(errors)}):")
    for q, e in errors: print(f"  {q}: {e}")

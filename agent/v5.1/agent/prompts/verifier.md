# Verifier — Physical sanity checks (T1 + T2)

This is internal documentation describing the checks performed by the
`verify_run` tool. The tool itself is mandatory before `submit`.

## T1 `vc_relax` checks

| Check | Threshold | Why |
|-------|-----------|-----|
| file present + ≥ 1 KB | hard | mirrors bench audit |
| `JOB DONE.` in last 64 KB | hard | mirrors bench audit |
| SCF converged | hard | electron loop reached etot_conv_thr |
| `max\|force\|` ≤ 1e-4 Ry/Bohr | hard | matches `forc_conv_thr`; loose forces ⇒ wrong relaxation minimum |
| `max\|σ_diag\|` ≤ 0.05 kbar | hard | matches `press_conv_thr`; loose stress ⇒ wrong cell |
| space group preserved (vs initial) | hard | bench scores `space_group_number` (0.25 of T1) |
| lattice drift ≤ 5 % | advisory | catches "ran the wrong material" |
| angles match initial within tol | hard for cubic, advisory else | 30° drift in cubic = symmetry break |

## T2 `bands_dos` checks

| Check | Threshold | Why |
|-------|-----------|-----|
| 4 files present + ≥ 1 KB each | hard | scf.out, bands.out, bands.dat, dos.dat |
| `JOB DONE.` on log files | hard | scf.out + bands.out (data files exempt) |
| SCF converged | hard | base of everything downstream |
| Fermi finite, \|E_F\| < 50 eV | hard | nan / garbage ⇒ scoring breaks |
| `nbnd` ≥ 4 (and reported) | hard | bands plot needs empty states |
| bands.dat parseable, non-empty | hard | scoring reads this directly |
| dos.dat shape > 10 rows, ≥ 2 cols | hard | curve_l2 score needs grid |
| dos covers E_F ± 2 eV | hard | gap and DOS curve depend on this window |

## Verdicts

- `pass` — all hard checks ✓, advisory checks may fail
- `soft_fail` — hard checks ✓ but at least one advisory failed (still submittable)
- `hard_fail` — at least one hard check ✗; you must fix before submit

`solve.py` re-runs `verify_run` on the final submission payload. If it
returns `hard_fail`, the agent gets one replay with the verifier complaint
appended to the prompt. Two replays max; after that, the original payload
is submitted with a `notes` field flagging the verifier complaint.

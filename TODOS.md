# TODOS

## Trailing stop immediate-trigger risk

**What:** `trail_stop = high_water_mark * (1 - trail_pct)` in `position_manager.py:234` could theoretically be set above `current_price` if `high_water_mark` jumped more than `trail_pct` worth of movement between ticks (e.g. a very fast option price spike and reversal).

**Why:** Same class of bug as the gatekeeper breakeven issue fixed on 2026-03-13 — a stop being set above the current price causes a stop-out on the very next tick. The trailing stop activates only after Tranche A closes, so it's lower urgency and hasn't been observed, but the pattern is present.

**Where:** `position_manager.py:234-243`

**Fix pattern:** Add `and trail_stop < current_price` guard (same approach as gatekeeper fix):
```python
if trail_stop > pos.current_stop and trail_stop < current_price:
```

**Pros:** Eliminates the latent risk; consistent stop-management contract across all stop update paths.
**Cons:** Very low real-world probability since `high_water_mark` is updated at the top of every `evaluate_position` call — it would require a massive single-tick reversal.

**Priority:** Low — not observed in paper trading. Revisit after 2 weeks of live data.
**Depends on:** None.

---

## Deduplicate _fmt_pnl / _pnl_str formatting functions

**What:** `eod_report._fmt_pnl()` (L344) and `autogex_dashboard._pnl_str()` (L179) are identical functions — both format a float as `+$X.XX` / `-$X.XX`. Neither imports from the other.

**Why:** If the display format ever needs to change (e.g. adding color codes, changing sign convention, rounding), the change must be made in two places and the divergence will likely go unnoticed.

**Where:** `eod_report.py:344-347`, `autogex_dashboard.py:179-182`

**Fix pattern:** Create `autogex_utils.py` (or similar) with a single `fmt_pnl()`, then import it in both modules. Alternatively, have `autogex_dashboard` import `_fmt_pnl` from `eod_report` since `eod_report` is already imported there as `_eod_report`.

**Pros:** Single source of truth for P&L display format; one change propagates everywhere.
**Cons:** Touches 3 files (the two callers + either a new utils module or an exposed import). Low risk.

**Priority:** Low — cosmetic DRY issue, no correctness risk until format diverges.
**Depends on:** None.

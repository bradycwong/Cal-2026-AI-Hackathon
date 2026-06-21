# Sample data (for testing protocol uploads & inventory additions)

Throwaway fixtures kept **on the side** of the live data in `backend/data/`. Use
them to exercise the "upload a protocol" and "add inventory items" flows without
hand-writing files each time. Every file matches the exact schema the loaders in
`backend/state.py` expect, so they load cleanly.

```
sample_data/
  protocols/
    gel_electrophoresis.yaml   # 7 steps, 3 timed (microwave / gel set / run)
    bca_protein_assay.yaml     # 6 steps, 1 timed (37 °C incubation)
    rna_extraction.yaml        # 6 steps, 3 timed (incubate / phase sep / spin)
  inventory_additions.csv      # 12 reagents used by the protocols above
```

None of these overlap with the four shipped protocols (`dna_extraction`,
`pcr_setup`, `bacterial_transformation`, `plasmid_miniprep`) or the existing
`backend/data/inventory.csv` rows, so adding them never collides.

## Protocol schema (YAML)

```yaml
protocol:
  id: gel_electrophoresis      # unique slug; dict key in state.protocols
  name: Gel Electrophoresis    # display name + load match
  version: "1.0"               # informational
  aliases: ["gel", "run a gel"]  # extra spoken/typed match terms (lowercased)
  steps:
    - id: 1                    # 1-based int
      text: "..."              # shown in the guide
      duration_s: 120          # null/omitted = manual advance; int = timed step
      timer_label: microwave   # label for the timed step's timer
      parameters: { voltage_v: 100 }  # free-form metadata
```

## Inventory schema (CSV)

Columns: `name,location,quantity_approx,notes` (notes may be empty). One reagent
per row; matched loosely by `find_inventory`.

## How to test

**Upload / import a protocol** — there is no upload endpoint yet (it's a deferred
organ; see `plan_3.txt`). Today the loader globs `backend/data/protocols/*.yaml`
at startup, so simulate an upload by dropping one in and restarting:

```bash
cp sample_data/protocols/gel_electrophoresis.yaml backend/data/protocols/
# restart uvicorn, then in the UI:  "load gel electrophoresis"
```

**Add inventory items** — append rows to the live CSV and restart:

```bash
tail -n +2 sample_data/inventory_additions.csv >> backend/data/inventory.csv
# restart uvicorn, then in the UI:  "where is the SYBR Safe"
```

> Tip: work on a copy of `backend/data/` if you don't want to mutate the shipped
> data. Restore with `git checkout backend/data/`.

### Validate they parse

```bash
python - <<'PY'
from pathlib import Path
from backend.state import load_protocol_file, load_inventory_file
for p in sorted(Path("sample_data/protocols").glob("*.yaml")):
    proto = load_protocol_file(p)
    print(proto.id, "->", len(proto.steps), "steps")
print("inventory rows:", len(load_inventory_file(Path("sample_data/inventory_additions.csv"))))
PY
```

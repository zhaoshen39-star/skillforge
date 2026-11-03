# Design notes

`tinycalc` uses a streaming parser: it never loads the whole file into memory.

## Edge cases

- Ragged rows are dropped by default; `--strict` turns that into an error.
- A UTF-8 BOM is stripped silently.
- Empty input yields `[]`.

## Testing

```bash
python -m pytest
```
